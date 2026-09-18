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


def _session(*, channel: str = "group", account: str = "bot", turn: str = "turn") -> Session:
    return cast(
        Session,
        SimpleNamespace(
            account=SimpleNamespace(platform="test", self_id=account),
            channel=SimpleNamespace(id=channel),
            user=SimpleNamespace(id="user"),
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
