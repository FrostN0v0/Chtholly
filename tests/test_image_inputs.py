"""Behavioral regressions for immutable turn-local image acquisition."""

from __future__ import annotations

from types import SimpleNamespace
import base64
from typing import cast
import asyncio
from pathlib import Path

import pytest
from arclet.entari import Session

from plugins.llm_chat import image_inputs as image_inputs_module
from plugins.llm_chat.image_inputs import ImageInputs, ImageInputError
from plugins.llm_chat.agent_attachments import resolve_agent_attachment

_PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=")


def _session(*, channel: str = "group", account: str = "bot", turn: str = "turn", user: str = "user") -> Session:
    return cast(
        Session,
        SimpleNamespace(
            account=SimpleNamespace(platform="test", self_id=account),
            channel=SimpleNamespace(id=channel),
            user=SimpleNamespace(id=user),
            event=SimpleNamespace(message=SimpleNamespace(id=turn)),
        ),
    )


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_cancel_shared_original_or_change_later_consumers(tmp_path: Path):
    session = _session()
    inputs = ImageInputs(attachment_root=tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()
    downloads = 0

    async def changing_source() -> bytes:
        nonlocal downloads
        downloads += 1
        started.set()
        await release.wait()
        return _PNG + str(downloads).encode()

    first = inputs.register(session, source="direct", key="same-locator", load=changing_source, index=1)
    second = inputs.register(session, source="quoted", key="same-locator", load=changing_source, index=2)
    cancelled = asyncio.create_task(inputs.resolve(session, first, purpose="inspect"))
    await started.wait()
    retained = asyncio.create_task(inputs.resolve(session, second, purpose="edit"))
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    release.set()
    editing = await retained
    sending = await inputs.resolve(session, first, purpose="send")
    collecting = await inputs.resolve(session, first, purpose="collect")
    assert downloads == 1
    assert editing.data is sending.data is collecting.data
    assert sending.data == _PNG + b"1"
    assert editing.source == "quoted"
    assert sending.source == "direct"
    assert sending.attachment is not None
    stored = resolve_agent_attachment(
        str(sending.attachment["attachment_ref"]),
        sending.mime,
        root=tmp_path,
    )
    assert stored.read_bytes() == sending.data
    await inputs.aclose()
    assert not stored.exists()


@pytest.mark.asyncio
async def test_failed_source_keeps_original_position_and_does_not_retry(tmp_path: Path):
    session = _session()
    inputs = ImageInputs(attachment_root=tmp_path)
    downloads = 0

    async def unavailable() -> bytes:
        nonlocal downloads
        downloads += 1
        raise OSError("gone")

    first = inputs.register(session, source="direct", key="first", load=unavailable, index=1)
    second = inputs.register_bytes(session, source="direct", key="second", data=_PNG, index=2)
    for purpose in ("inspect", "send"):
        with pytest.raises(ImageInputError):
            await inputs.resolve(session, first, purpose=purpose)
    snapshot = await inputs.resolve(session, inputs.input_ref(2), purpose="edit")
    assert downloads == 1
    assert snapshot.image_ref == second
    assert inputs.input_views() == [
        {"index": 1, "image_ref": first, "source": "direct", "status": "unavailable"},
        {"index": 2, "image_ref": second, "source": "direct", "status": "ready"},
    ]
    await inputs.aclose()


@pytest.mark.asyncio
async def test_identical_bytes_share_capacity_but_never_source_permissions(tmp_path: Path):
    session = _session()
    inputs = ImageInputs(attachment_root=tmp_path, max_bytes=len(_PNG))
    direct = inputs.register_bytes(session, source="direct", key="direct", data=_PNG, index=1)
    web = inputs.register_bytes(session, source="web", key="web", data=_PNG)
    source = await inputs.resolve(session, direct, purpose="send")
    reference = await inputs.resolve(session, web, purpose="reference")
    assert source.data is reference.data
    with pytest.raises(ImageInputError):
        await inputs.resolve(session, web, purpose="send")
    with pytest.raises(ImageInputError):
        await inputs.resolve(session, web, purpose="collect")
    with pytest.raises(ImageInputError, match="capacity"):
        inputs.register_bytes(session, source="avatar", key="avatar", data=_PNG + b"different")
    assert (await inputs.resolve(session, direct, purpose="edit")).data == _PNG
    await inputs.aclose()


@pytest.mark.asyncio
async def test_audit_failure_does_not_block_safe_pixels_or_persist_capabilities(tmp_path: Path, monkeypatch):
    def failed_store(*args, **kwargs):
        raise OSError("read-only attachment storage")

    monkeypatch.setattr(image_inputs_module, "store_agent_attachment", failed_store)
    session = _session()
    inputs = ImageInputs(attachment_root=tmp_path)
    ref = inputs.register_bytes(session, source="quoted", key="quoted", data=_PNG, index=1)
    snapshot = await inputs.resolve(session, ref, purpose="edit")
    assert snapshot.data == _PNG
    assert snapshot.attachment is None
    assert snapshot.audit_status == "unrecorded"
    assert inputs.input_audit_views() == [
        {
            "index": 1,
            "source": "quoted",
            "status": "ready",
            "audit_status": "unrecorded",
        }
    ]
    await inputs.aclose()


@pytest.mark.asyncio
async def test_close_cancels_and_drains_acquisition_and_rejects_late_use(tmp_path: Path):
    session = _session()
    inputs = ImageInputs(attachment_root=tmp_path)
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def blocked() -> bytes:
        started.set()
        try:
            await asyncio.Event().wait()
            return _PNG
        finally:
            stopped.set()

    ref = inputs.register(session, source="forward", key="forward", load=blocked, index=1)
    waiter = asyncio.create_task(inputs.resolve(session, ref, purpose="inspect"))
    await started.wait()
    await inputs.aclose()
    assert stopped.is_set()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    with pytest.raises(ImageInputError):
        await inputs.resolve(session, ref, purpose="send")
    assert inputs.drain_inspections() == ()


@pytest.mark.asyncio
async def test_refs_reject_other_channel_account_turn_and_registry(tmp_path: Path):
    session = _session()
    inputs = ImageInputs(attachment_root=tmp_path)
    other_inputs = ImageInputs(attachment_root=tmp_path)
    ref = inputs.register_bytes(session, source="avatar", key="avatar", data=_PNG)
    for stranger in (_session(channel="elsewhere"), _session(account="other"), _session(turn="next")):
        with pytest.raises(ImageInputError):
            await inputs.resolve(stranger, ref, purpose="edit")
    with pytest.raises(ImageInputError):
        await other_inputs.resolve(session, ref, purpose="edit")
    snapshot = await inputs.resolve(session, ref, purpose="inspect")
    with pytest.raises(ImageInputError):
        other_inputs.queue_inspection(snapshot)
    await inputs.aclose()
    await other_inputs.aclose()


@pytest.mark.asyncio
async def test_committed_audit_survives_turn_cleanup_without_retaining_pixels(tmp_path: Path):
    session = _session()
    inputs = ImageInputs(attachment_root=tmp_path)
    ref = inputs.register_bytes(session, source="direct", key="original", data=_PNG, index=1)
    snapshot = await inputs.resolve(session, ref, purpose="inspect")
    assert snapshot.attachment is not None
    path = resolve_agent_attachment(str(snapshot.attachment["attachment_ref"]), snapshot.mime, root=tmp_path)
    inputs.commit_input_audit()
    await inputs.aclose()
    assert path.read_bytes() == _PNG
    with pytest.raises(ImageInputError):
        await inputs.resolve(session, ref, purpose="send")


@pytest.mark.asyncio
async def test_nested_forward_pixels_keep_node_identity_and_failed_image_positions(tmp_path: Path):
    import json

    from arclet.entari import MessageChain
    from satori.element import Custom

    from plugins.llm_chat.config import LLMChatConfig
    from plugins.llm_chat.chat_context import build_multimodal_user_content
    from plugins.llm_chat.image_inputs import image_inputs_scope
    from plugins.llm_chat.forward_context import resolve_merged_forward_messages

    session = _session()
    runtime_session = cast(SimpleNamespace, session)
    runtime_session.elements = MessageChain()
    runtime_session.quote = SimpleNamespace(children=[Custom("onebot:forward", {"id": "root"})])
    runtime_session.reply = None
    payloads = {
        "root": {
            "messages": [
                {
                    "sender": {"user_id": "one", "nickname": "Same name"},
                    "message": [
                        {"type": "image", "data": {"url": "missing"}},
                        {"type": "image", "data": {"url": "pixels"}},
                        {"type": "forward", "data": {"id": "nested"}},
                    ],
                }
            ]
        },
        "nested": {
            "messages": [
                {
                    "sender": {"user_id": "two", "nickname": "Same name"},
                    "message": [
                        {"type": "image", "data": {"url": "pixels"}},
                    ],
                }
            ]
        },
    }
    downloads: list[str] = []

    async def internal(action: str, *, message_id: str):
        assert action == "get_forward_msg"
        return payloads[message_id]

    async def download(source: str) -> bytes:
        downloads.append(source)
        if source == "missing":
            raise OSError("unavailable")
        return _PNG

    runtime_session.internal = internal
    runtime_session.download = download
    inputs = ImageInputs(attachment_root=tmp_path)
    config = LLMChatConfig(merged_forward_max_images=4)
    with image_inputs_scope(inputs):
        messages = await resolve_merged_forward_messages(config, session, lambda _: None)
        assert downloads == []
        assert messages[0]["speaker"] == messages[1]["speaker"]
        assert messages[0].get("speaker_ref") != messages[1].get("speaker_ref")
        assert messages[0].get("node_ref") is not None
        assert messages[1].get("parent_node_ref") == messages[0].get("node_ref")
        content, stored = await build_multimodal_user_content(
            config,
            session,
            "Current user",
            "What is shown?",
            lambda _: None,
            messages,
        )
    assert isinstance(content, list)
    images = [part for part in content if part["type"] == "image_url"]
    assert len(images) == 2
    assert all(base64.b64decode(part["image_url"]["url"].split(",", 1)[1]) == _PNG for part in images)
    assert downloads == ["missing", "pixels"]
    original_images = messages[0].get("images", [])
    assert original_images[0]["index"] == 1
    assert original_images[0]["status"] == "unavailable"
    second = original_images[1]
    assert second["index"] == 2
    assert second["status"] == "ready"
    with pytest.raises(ImageInputError):
        await inputs.resolve(session, str(second["image_ref"]), purpose="collect")
    stored_nodes = json.loads(stored)["forwarded_messages"]
    assert all("image_ref" not in image for node in stored_nodes for image in node.get("images", []))
    await inputs.aclose()


@pytest.mark.asyncio
async def test_admitted_input_shares_original_snapshot_only_with_root_and_own_event(tmp_path: Path):
    from arclet.letoderea.context import Contexts

    from plugins.llm_chat.steering import SteeringState

    root = _session()
    extra = _session(turn="extra")
    later = _session(turn="later")
    state = SteeringState(root, Contexts())
    inputs = ImageInputs(attachment_root=tmp_path)
    first = inputs.register_bytes(root, source="direct", key="first", data=_PNG, index=1)
    inputs.admit_input(root, state=state)
    assert state.receive(extra, Contexts())
    inputs.admit_input(extra, state=state)
    downloads = 0

    async def acquire() -> bytes:
        nonlocal downloads
        downloads += 1
        return _PNG + b"extra"

    added = inputs.register(extra, source="direct", key="new", load=acquire, index=1)
    inspected = await inputs.resolve(extra, added, purpose="inspect")
    assert await inputs.resolve(root, added, purpose="edit") is inspected
    assert await inputs.resolve(root, added, purpose="send") is inspected
    assert await inputs.resolve(extra, added, purpose="collect") is inspected
    assert downloads == 1
    assert inputs.input_ref(1) == first
    assert inputs.input_ref(2) == added
    assert inputs.input_views(session=extra) == [
        {
            "index": 2,
            "image_ref": added,
            "source": "direct",
            "status": "ready",
            "steer_sequence": 2,
            "original_index": 1,
        }
    ]
    assert state.receive(later, Contexts())
    inputs.admit_input(later, state=state)
    for stranger in (
        later,
        _session(turn="extra"),
        _session(turn="historical"),
        _session(turn="extra", user="other"),
        _session(turn="extra", channel="elsewhere"),
    ):
        with pytest.raises(ImageInputError):
            await inputs.resolve(stranger, added, purpose="edit")
    with pytest.raises(ImageInputError):
        inputs.admit_input(_session(turn="historical"), state=state)
    with pytest.raises(ImageInputError):
        await inputs.resolve(extra, first, purpose="send")
    assert (await inputs.resolve(root, first, purpose="send")).data == _PNG
    await inputs.aclose()
    await state.aclose()


@pytest.mark.asyncio
async def test_supplement_attachment_limit_is_per_input_not_global_position(tmp_path: Path):
    from arclet.letoderea.context import Contexts

    from plugins.llm_chat.steering import SteeringState
    from plugins.llm_chat.agent_attachments import capture_user_input_images

    root = _session()
    extra = _session(turn="extra")
    state = SteeringState(root, Contexts())
    inputs = ImageInputs(attachment_root=tmp_path)
    for index in range(1, 7):
        inputs.register_bytes(root, source="direct", key=("root", index), data=_PNG, index=index)
    inputs.admit_input(root, state=state)
    original = await capture_user_input_images(root, inputs)
    inputs.commit_input_audit(session=root)
    assert state.receive(extra, Contexts())
    inputs.admit_input(extra, state=state)
    ref = inputs.register_bytes(extra, source="direct", key="extra", data=_PNG, index=1)
    added = await capture_user_input_images(extra, inputs)
    assert len(original) == 6
    assert len(added) == 1
    assert added[0]["index"] == 7
    assert added[0]["original_index"] == 1
    assert added[0]["steer_sequence"] == 2
    assert added[0]["audit_status"] == "recorded"
    snapshot = await inputs.resolve(root, ref, purpose="inspect")
    assert snapshot.attachment is not None
    path = resolve_agent_attachment(str(snapshot.attachment["attachment_ref"]), snapshot.mime, root=tmp_path)
    await inputs.aclose()
    assert not path.exists()
    assert all(
        resolve_agent_attachment(str(item["attachment_ref"]), str(item["mime"]), root=tmp_path).exists()
        for item in original
    )
    await state.aclose()


@pytest.mark.asyncio
async def test_new_payload_never_duplicates_earlier_image_notes_or_pixels(tmp_path: Path):
    import json

    from arclet.entari import MessageChain
    from arclet.letoderea.context import Contexts

    from plugins.llm_chat.config import LLMChatConfig
    from plugins.llm_chat.steering import SteeringState
    from plugins.llm_chat.chat_context import build_multimodal_user_content
    from plugins.llm_chat.image_inputs import image_inputs_scope

    root = _session()
    extra = _session(turn="extra")
    for session in (root, extra):
        runtime = cast(SimpleNamespace, session)
        runtime.elements = MessageChain()
        runtime.quote = None
        runtime.reply = None
    state = SteeringState(root, Contexts())
    inputs = ImageInputs(attachment_root=tmp_path)
    old = inputs.register_bytes(root, source="direct", key="old", data=_PNG, index=1)
    inputs.admit_input(root, state=state)
    assert state.receive(extra, Contexts())
    inputs.admit_input(extra, state=state)
    new = inputs.register_bytes(extra, source="direct", key="new", data=_PNG, index=1)
    with image_inputs_scope(inputs):
        content, stored = await build_multimodal_user_content(
            LLMChatConfig(), extra, "Alice", "Use this instead", lambda _: None
        )
    assert isinstance(content, list)
    payload = json.loads(content[0]["text"])
    assert [image["image_ref"] for image in payload["input_images"]] == [new]
    assert old not in content[0]["text"]
    assert len([part for part in content if part["type"] == "image_url"]) == 1
    assert stored == payload["content"]
    await inputs.aclose()
    await state.aclose()
