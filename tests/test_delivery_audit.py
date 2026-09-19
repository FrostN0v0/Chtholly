"""Confirmed output audit at the real Entari message_create/SendResponse boundary."""

from __future__ import annotations

import json
from types import SimpleNamespace
import base64
import asyncio
from pathlib import Path
from datetime import datetime, timezone, timedelta
from dataclasses import asdict

import pytest
from satori import (
    At,
    File,
    Link,
    Text,
    User,
    Audio,
    Event,
    Image,
    Login,
    Quote,
    Video,
    Author,
    Channel,
    Message,
    EventType,
    ChannelType,
)
from satori.const import Api
from satori.model import MessageObject
from arclet.entari import Session, MessageChain, MessageCreatedEvent
from satori.client import Account, ApiInfo
from satori.element import Custom
from arclet.entari.config import EntariConfig
from arclet.entari.session import COMPONENTS, EntariProtocol

if not hasattr(EntariConfig, "instance"):
    setattr(
        EntariConfig, "instance", EntariConfig.load(Path(__file__).resolve().parents[1] / "entari.full.example.yml")
    )

from plugins.llm_chat import delivery_audit, context_builder
from plugins.llm_chat.models import AgentTurn, AgentEvent
from plugins.llm_chat.core.tool_trace import llm_chat_tool_execution_scope
from plugins.llm_chat.core.agent_trace import AgentTurnRecorder
from plugins.llm_chat.agent_attachments import resolve_agent_attachment

_PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=")


def _session(account, message_id="incoming-1", channel="group-1", user="actor-1"):
    origin = Event(
        EventType.MESSAGE_CREATED,
        datetime.now(),
        account.self_info,
        channel=Channel(channel, ChannelType.TEXT),
        user=User(user, name="Alice"),
        message=MessageObject(message_id, "render this"),
    )
    return Session(account, MessageCreatedEvent(account, origin))


@pytest.fixture
async def transport(monkeypatch):
    login = Login(sn=0, platform="onebot", adapter="onebot", user=User("audit-bot"))
    account = Account(login, ApiInfo(), [], EntariProtocol)
    state = SimpleNamespace(outcome="success", wire=[], gate=None, entered=asyncio.Event())

    async def call_api(action, params):
        if action == Api.CHANNEL_GET:
            return {"id": params["channel_id"], "type": ChannelType.TEXT.value}
        assert action == Api.MESSAGE_CREATE
        state.wire.append(params)
        state.entered.set()
        if state.gate is not None:
            await state.gate.wait()
        if state.outcome == "failed":
            raise RuntimeError("transport refused message")
        if state.outcome == "empty":
            return []
        return [{"id": f"sent-{len(state.wire)}", "content": params["content"]}]

    monkeypatch.setattr(account.protocol, "call_api", call_api)
    dispose = delivery_audit.install_delivery_audit()
    try:
        yield SimpleNamespace(account=account, session=_session(account), state=state)
    finally:
        await dispose()
        await account.protocol.session.close()


def _deliveries(recorder):
    return [event for event in recorder.events if event.event_type == "message_delivery"]


@pytest.mark.asyncio
async def test_rendered_png_and_text_keep_actual_receipt_time_and_private_bytes(transport, tmp_path, monkeypatch):
    clock = [1.0]
    origin = datetime(2026, 9, 10)

    class ReceiptClock:
        @staticmethod
        def utcnow():
            return origin + timedelta(seconds=clock[0])

    monkeypatch.setattr(delivery_audit, "datetime", ReceiptClock)
    monkeypatch.setattr(delivery_audit, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    stored = []

    async def sink(events):
        clock[0] += 60  # Persistence must not inflate this receipt's duration.
        stored.extend(events)

    async def render(_attrs, _children, _session):
        return MessageChain([Image.of(raw=_PNG), Text("rendered caption")])

    monkeypatch.setitem(COMPONENTS, "component:audit-renderer", render)
    recorder = AgentTurnRecorder(sink=sink)
    warnings = []
    with delivery_audit.delivery_audit_scope(transport.session, warnings.append, attachment_root=tmp_path) as audit:
        audit.bind(recorder)
        clock[0] = 6.0
        with llm_chat_tool_execution_scope("send_msg-execution"):
            result = await transport.session.send(MessageChain([Custom("audit-renderer")]))
        await audit.drain()
        first = _deliveries(recorder)[0]
        assert result[0].id == "sent-1"
        assert first in stored
        assert first.created_at == origin + timedelta(seconds=6)
        assert first.duration_ms == 5000
        assert first.payload["confirmed_at"] == first.created_at.replace(tzinfo=timezone.utc).isoformat()
        attachment = first.payload["attachments"][0]
        path = resolve_agent_attachment(attachment["attachment_ref"], attachment["mime"], root=tmp_path)
        assert path.read_bytes() == _PNG
        assert path.name.startswith("output_")
        assert attachment["mime"] == "image/png"
        assert first.execution_ref == "send_msg-execution"
        assert first.payload["content"] == "[图片 1]rendered caption"
        assert "<img" in transport.state.wire[0]["content"]
        clock[0] = 70.0
        await transport.session.send(MessageChain([Text("final outgoing reply")]))
        await audit.drain()
    first, final = _deliveries(recorder)
    assert final.execution_ref == ""
    assert final.payload["content"] == "final outgoing reply"
    assert final.duration_ms == 69000
    assert all(event.status == event.effect == "confirmed" for event in (first, final))
    assert all(not event.model_visible for event in recorder.events)
    assert "data:image" not in json.dumps([event.payload for event in stored])
    assert str(tmp_path) not in json.dumps([event.payload for event in stored])
    assert warnings == []
    # Exercise the real context projection with the actual captured private events.
    rows = []
    for event in recorder.events:
        values = asdict(event)
        values["payload_json"] = json.dumps(values.pop("payload"))
        rows.append(AgentEvent(turn_id=1, event_ref=f"event-{event.sequence}", **values))
    assert context_builder._turn_messages(AgentTurn(id=1), rows, inline_chars=4000, validators={}) == []


@pytest.mark.asyncio
async def test_prebind_receipts_and_failed_or_empty_sends_preserve_only_confirmed_prefix(transport, tmp_path):
    recorder = AgentTurnRecorder()
    with delivery_audit.delivery_audit_scope(transport.session, lambda _: None, attachment_root=tmp_path) as audit:
        await transport.session.send(MessageChain([Image.of(raw=_PNG)]))
        await audit.drain()
        initial_time = audit.recorder.events[0].created_at
        audit.bind(recorder)
        await transport.session.send(MessageChain([Text("confirmed prefix")]))
        transport.state.outcome = "failed"
        with pytest.raises(RuntimeError, match="transport refused"):
            await transport.session.send(MessageChain([Text("not delivered")]))
        transport.state.outcome = "empty"
        assert await transport.session.send(MessageChain([Text("also not delivered")])) == []
        await audit.drain()
    assert [event.event_type for event in recorder.events] == ["turn_timing", "message_delivery", "message_delivery"]
    assert _deliveries(recorder)[0].created_at == initial_time
    assert _deliveries(recorder)[1].payload["content"] == "confirmed prefix"
    assert len(list(tmp_path.iterdir())) == 1


@pytest.mark.asyncio
async def test_cancel_before_transport_confirmation_records_no_delivery(transport, tmp_path):
    recorder = AgentTurnRecorder()
    transport.state.gate = asyncio.Event()
    with delivery_audit.delivery_audit_scope(transport.session, lambda _: None, attachment_root=tmp_path) as audit:
        audit.bind(recorder)
        sending = asyncio.create_task(transport.session.send(MessageChain([Image.of(raw=_PNG)])))
        await transport.state.entered.wait()
        sending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sending
        await audit.drain()
    assert _deliveries(recorder) == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_generation_context_and_source_event_isolate_concurrent_sends(transport, tmp_path):
    first = transport.session
    second = _session(transport.account, message_id="incoming-2", user="actor-2")
    unrelated = _session(transport.account, message_id="unrelated")
    first_recorder, second_recorder = AgentTurnRecorder(), AgentTurnRecorder()
    barrier = asyncio.Event()

    async def send(session, recorder, text):
        with delivery_audit.delivery_audit_scope(session, lambda _: None, attachment_root=tmp_path) as audit:
            audit.bind(recorder)
            await barrier.wait()
            # Same channel/account alone is insufficient to attribute another handler's send.
            await unrelated.send(MessageChain([Text("unrelated outgoing")]))
            await session.send(MessageChain([Text(text)]))
            await audit.drain()
            return audit

    one = asyncio.create_task(send(first, first_recorder, "first user output"))
    two = asyncio.create_task(send(second, second_recorder, "second user output"))
    barrier.set()
    await asyncio.gather(one, two)
    assert [event.payload["content"] for event in _deliveries(first_recorder)] == ["first user output"]
    assert [event.payload["content"] for event in _deliveries(second_recorder)] == ["second user output"]
    assert delivery_audit.current_delivery_audit() is None
    # Entari reconstructed a different Session internally; the source event matched.
    assert len(transport.state.wire) == 4


@pytest.mark.asyncio
async def test_closed_inherited_generation_cannot_attach_late_child_sends(transport, tmp_path):
    recorder = AgentTurnRecorder()
    released = asyncio.Event()

    async def late_send():
        await released.wait()
        await transport.session.send(MessageChain([Text("after generation ended")]))

    with delivery_audit.delivery_audit_scope(transport.session, lambda _: None, attachment_root=tmp_path) as audit:
        audit.bind(recorder)
        task = asyncio.create_task(late_send())
    released.set()
    await task
    assert _deliveries(recorder) == []


@pytest.mark.asyncio
async def test_attachment_and_optional_sink_cancellation_do_not_fail_successful_send(transport, tmp_path, monkeypatch):
    warnings = []
    persisted = []
    cancelled = True

    async def sink(events):
        nonlocal cancelled
        if cancelled:
            cancelled = False
            raise asyncio.CancelledError
        persisted.extend(events)

    def storage_failure(*_args, **_kwargs):
        raise OSError("private storage unavailable")

    monkeypatch.setattr(delivery_audit, "store_agent_attachment", storage_failure)
    recorder = AgentTurnRecorder(sink=sink)
    with delivery_audit.delivery_audit_scope(transport.session, warnings.append, attachment_root=tmp_path) as audit:
        audit.bind(recorder)
        assert await transport.session.send(MessageChain([Image.of(raw=_PNG), Text("still delivered")]))
        await audit.drain()
        first = _deliveries(recorder)[0]
        assert first.payload["attachments"] == []
        assert first.payload["media"][0]["capture_status"] == "failed"
        assert first.payload["capture_status"] == "partial"
        assert await transport.session.send(MessageChain([Text("later confirmed")]))
        await audit.drain()
    assert [event.payload["content"] for event in persisted if event.event_type == "message_delivery"] == [
        "[图片 1]still delivered",
        "later confirmed",
    ]
    assert len(transport.state.wire) == 2
    assert warnings
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_pending_audit_never_blocks_sender_or_swallows_generation_cancellation(transport, tmp_path):
    entered, released, returned = asyncio.Event(), asyncio.Event(), asyncio.Event()
    persisted = []

    async def sink(events):
        entered.set()
        await released.wait()
        persisted.extend(events)

    recorder = AgentTurnRecorder(sink=sink)

    async def generation():
        with delivery_audit.delivery_audit_scope(transport.session, lambda _: None, attachment_root=tmp_path) as audit:
            audit.bind(recorder)
            try:
                result = await transport.session.send(MessageChain([Image.of(raw=_PNG)]))
                assert result[0].id == "sent-1"
                returned.set()
                await asyncio.Event().wait()
                await transport.session.send(MessageChain([Text("must not send after cancellation")]))
            finally:
                await audit.drain()

    sending = asyncio.create_task(generation())
    try:
        # Receipt return must not depend on releasing the optional storage sink.
        await asyncio.wait_for(returned.wait(), timeout=2)
        await entered.wait()
        sending.cancel()
        released.set()
        with pytest.raises(asyncio.CancelledError):
            await sending
    finally:
        released.set()
        if not sending.done():
            sending.cancel()
        await asyncio.gather(sending, return_exceptions=True)
    delivery = _deliveries(recorder)[0]
    assert delivery in persisted
    assert delivery.status == delivery.effect == "confirmed"
    attachment = delivery.payload["attachments"][0]
    assert (
        resolve_agent_attachment(attachment["attachment_ref"], attachment["mime"], root=tmp_path).read_bytes() == _PNG
    )
    assert len(transport.state.wire) == 1


@pytest.mark.asyncio
async def test_projection_keeps_forward_order_and_never_labels_quote_images_as_sent(transport, tmp_path):
    recorder = AgentTurnRecorder()
    payload = MessageChain(
        [
            Quote("quoted-id", content=[Image.of(raw=_PNG), Text("quoted content")]),
            Message(
                forward=True,
                content=[
                    Message(content=[Author("private-author-id", name="Bob"), Text("first child"), At("actor-1")]),
                    Message(
                        content=[
                            Text(
                                "second child https://example.org/docs "
                                "https://example.com/private?token=hidden internal:secret-reference"
                            ),
                            Text(
                                " https://example.org/p/ABCDEFGHIJKLMNOPQRSTUVWX "
                                "https://example.org/image.png?token=hidden "
                            ),
                            Link("https://example.org/guide"),
                            Image.of(raw=_PNG),
                        ]
                    ),
                ],
            ),
            Audio.of(url="file:///private/audio.wav"),
            File.of(url="https://example.com/private-file?token=hidden"),
            Video.of(url="internal:private-video"),
        ]
    )
    with delivery_audit.delivery_audit_scope(transport.session, lambda _: None, attachment_root=tmp_path) as audit:
        audit.bind(recorder)
        await transport.session.send(payload)
        await audit.drain()
    event = _deliveries(recorder)[0]
    text = event.payload["content"]
    assert text.index("quoted content") < text.index("first child") < text.index("second child")
    assert "@Alice" in text
    assert "Bob: " in text
    assert "https://example.org/docs" in text
    assert "https://example.org/guide" in text
    assert "https://example.com/private?token=[REDACTED]" in text
    assert len(event.payload["attachments"]) == 1
    assert event.payload["attachments"][0]["index"] == 1
    assert {item["kind"] for item in event.payload["media"]} == {"audio", "file", "video"}
    serialized = json.dumps(event.payload)
    assert all(
        secret not in serialized
        for secret in (
            "actor-1",
            "private-author-id",
            "quoted-id",
            "hidden",
            "secret-reference",
            "ABCDEFGHIJKLMNOPQRSTUVWX",
            "image.png",
            "file://",
            "internal:",
            "base64",
        )
    )


@pytest.mark.asyncio
async def test_remote_capture_budget_keeps_inline_images_and_never_uses_arbitrary_download(
    transport, tmp_path, monkeypatch
):
    calls = []
    # Inline disk scheduling must not exhaust the intentionally tiny remote timeout.
    clock = [0.0]

    async def remote(source):
        calls.append(source)
        clock[0] = 1.0
        await asyncio.Event().wait()

    async def forbidden_download(*_args, **_kwargs):
        raise AssertionError("audit must not use unrestricted Session.download")

    monkeypatch.setattr(delivery_audit, "_fetch_public_direct_image", remote)
    monkeypatch.setattr(delivery_audit, "_REMOTE_CAPTURE_SECONDS", 0.01)
    monkeypatch.setattr(delivery_audit, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(Session, "download", forbidden_download)
    recorder = AgentTurnRecorder()
    with delivery_audit.delivery_audit_scope(transport.session, lambda _: None, attachment_root=tmp_path) as audit:
        audit.bind(recorder)
        await transport.session.send(
            MessageChain(
                [
                    Image.of(raw=_PNG),
                    Image.of(url="https://example.com/output.png"),
                    Image.of(raw=_PNG),
                    Image.of(url="file:///private/secret.png"),
                    Image.of(url="internal:secret"),
                ]
            )
        )
        await transport.session.send(MessageChain([Text("next confirmed bubble")]))
        await audit.drain()
    delivery = _deliveries(recorder)[0]
    assert [item["index"] for item in delivery.payload["attachments"]] == [1, 3]
    assert calls == ["https://example.com/output.png"]
    assert len(delivery.payload["media"]) == 3
    assert delivery.payload["capture_status"] == "partial"
    assert [event.payload["content"] for event in _deliveries(recorder)][1:] == ["next confirmed bubble"]


@pytest.mark.asyncio
async def test_preappend_failure_compensates_only_new_orphan_files(transport, tmp_path, monkeypatch):
    recorder = AgentTurnRecorder()
    with delivery_audit.delivery_audit_scope(transport.session, lambda _: None, attachment_root=tmp_path) as audit:
        audit.bind(recorder)
        await transport.session.send(MessageChain([Image.of(raw=_PNG)]))
        await audit.drain()
        existing = list(tmp_path.iterdir())
        previous_append = recorder.append

        def refuse_new_delivery(event_type, **kwargs):
            if event_type == "message_delivery":
                raise OSError("append unavailable")
            return previous_append(event_type, **kwargs)

        monkeypatch.setattr(recorder, "append", refuse_new_delivery)
        assert await transport.session.send(MessageChain([Image.of(raw=_PNG)]))
        await audit.drain()
    assert list(tmp_path.iterdir()) == existing
    assert existing[0].read_bytes() == _PNG
    assert len(_deliveries(recorder)) == 1
    assert len(transport.state.wire) == 2


@pytest.mark.asyncio
async def test_unbound_scope_discards_attachments_even_when_capture_finishes_after_close(
    transport, tmp_path, monkeypatch
):
    entered, released = asyncio.Event(), asyncio.Event()

    async def remote(_source):
        entered.set()
        await released.wait()
        return _PNG, "image/png"

    monkeypatch.setattr(delivery_audit, "_fetch_public_direct_image", remote)
    try:
        with delivery_audit.delivery_audit_scope(transport.session, lambda _: None, attachment_root=tmp_path) as audit:
            await transport.session.send(
                MessageChain([Image.of(raw=_PNG), Image.of(url="https://example.com/output.png")])
            )
            await entered.wait()
        released.set()
        await audit.drain()
    finally:
        released.set()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_private_url_receipt_never_opens_an_audit_network_client(transport, tmp_path, monkeypatch):
    from plugins.llm_chat.web import reference_capture

    opened = []

    def forbidden_client(*_args, **_kwargs):
        opened.append(True)
        raise AssertionError("private source must be rejected before opening a client")

    monkeypatch.setattr(reference_capture, "ClientSession", forbidden_client)
    recorder = AgentTurnRecorder()
    with delivery_audit.delivery_audit_scope(transport.session, lambda _: None, attachment_root=tmp_path) as audit:
        audit.bind(recorder)
        await transport.session.send(MessageChain([Image.of(url="http://127.0.0.1/secret.png")]))
        await audit.drain()
    delivery = _deliveries(recorder)[0]
    assert delivery.status == "confirmed"
    assert delivery.payload["attachments"] == []
    assert delivery.payload["media"][0]["capture_status"] == "failed"
    assert opened == []
    assert list(tmp_path.iterdir()) == []
