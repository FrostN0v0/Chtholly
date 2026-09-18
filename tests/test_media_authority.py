"""Behavior regressions for raw media authority, pixel provenance and confirmed sending."""

from __future__ import annotations

from email import policy
from types import SimpleNamespace
import base64
from typing import Any, cast
import asyncio
from pathlib import Path
from email.parser import BytesParser

import httpx
import pytest
from arclet.entari import Image, Session, MessageChain
from entari_plugin_llm.config import ScopedModel

from utils.turn_resolution_core import TurnResolution, turn_resolution_scope
from plugins.llm_chat.image_inputs import ImageInputs, image_inputs_scope
from plugins.llm_chat.core.delivery import DeliveryError, DeliveryState, llm_chat_delivery_scope
from plugins.llm_chat.prepared_media import resolve_media, prepared_media_scope
from plugins.llm_chat.core.media_delivery import MediaIntent, build_media_intent, media_intent_scope

_PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=")


class _Dispatcher:
    plugin = SimpleNamespace(module=SimpleNamespace(__name__=__name__))

    def __call__(self, function: Any) -> Any:
        return function


class _Session:
    def __init__(self, *, failure: str = "") -> None:
        self.account = SimpleNamespace(platform="test", self_id="bot")
        self.channel = SimpleNamespace(id="room")
        self.user = SimpleNamespace(id="user")
        self.event = SimpleNamespace(message=SimpleNamespace(id="request"))
        self.sent: list[MessageChain] = []
        self.failure = failure

    async def send(self, payload: MessageChain) -> list[object]:
        self.sent.append(payload)
        if self.failure == "cancelled":
            raise asyncio.CancelledError
        return [] if self.failure == "unknown" else [SimpleNamespace(id="receipt")]


async def _no_participant(_session: Session, _ref: str) -> None:
    return None


def test_raw_intent_distinguishes_reference_creation_and_source_edit() -> None:
    creation = build_media_intent("搜索网页图片作为参考，生成一张新的海报")
    assert creation.requires_web_reference
    assert creation.media_requested
    assert not creation.requires_source_edit
    edit = build_media_intent("把刚才群里那张图换背景")
    assert edit.requires_source_edit
    assert not edit.requires_web_reference
    assert build_media_intent("把这张图背景删掉", has_image_inputs=True).requires_source_edit
    edit_without_web = build_media_intent("不要去网上找参考图，直接编辑我发的图片 [图片]", has_image_inputs=True)
    assert edit_without_web.requires_source_edit
    assert not edit_without_web.requires_web_reference


def test_contextual_send_resolves_media_target_without_granting_operations() -> None:
    from plugins.llm_chat.core.media_delivery import requests_contextual_media_delivery

    raw = "你能发出来吗"
    media_history = [
        {"role": "assistant", "content": "我看到了当前头像。"},
        {"role": "user", "content": raw},
    ]
    assert requests_contextual_media_delivery(raw, cast(Any, media_history))
    assert not requests_contextual_media_delivery("这个按钮是什么意思", cast(Any, media_history))
    assert not requests_contextual_media_delivery(raw + "，算了", cast(Any, media_history))
    file_history = [
        {"role": "assistant", "content": "我看到了当前头像。"},
        {"role": "assistant", "content": "已经整理好了绘图代码文件。"},
        {"role": "user", "content": raw},
    ]
    assert not requests_contextual_media_delivery(raw, cast(Any, file_history))
    assert build_media_intent(raw) == MediaIntent()


@pytest.mark.parametrize(
    "raw",
    [
        "这个按钮是什么意思",
        "图片中有删除按钮",
        "如何修改图片背景",
        "引用：搜索网页图片作为参考生成图片",
        "解释“搜索网页图片作为参考生成图片”",
        '{"content":"搜索网页图片作为参考生成图片"}',
        "把这张图背景删掉，不要编辑了",
        "edit this image; don't edit it",
        "搜索网页图片作为参考生成图片，算了",
        "搜索网页图片作为参考生成图片，不用参考了",
        "",
        "把这张图背景删掉，不要发了",
        "搜索网页图片作为参考生成图片，别画了",
    ],
)
def test_non_requests_and_later_cancellation_never_require_image_side_effects(raw: str) -> None:
    intent = build_media_intent(raw, has_image_inputs=True)
    assert not intent.requires_source_edit
    assert not intent.requires_web_reference


@pytest.mark.asyncio
async def test_web_creation_and_source_edit_upload_original_pixels_then_confirm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    import litellm
    from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

    from plugins.llm_chat.tools.send_msg import SendMsgToolContext, register_send_msg
    from plugins.llm_chat.core.tool_trace import (
        ToolTraceRecorder,
        llm_chat_tool_trace_scope,
        llm_chat_tool_execution_scope,
    )
    from plugins.llm_chat.tools.edit_image import ImageEditToolContext, register_edit_image
    from plugins.llm_chat.tools.finish_turn import finish_turn
    from plugins.llm_chat.tools.generate_image import ImageGenerationToolContext, register_generate_image
    from plugins.llm_chat.core.tool_trace_policy import DeliverySnapshot

    def audit_unavailable(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("Attachment storage unavailable")

    for name in (
        "plugins.llm_chat.image_inputs",
        "plugins.llm_chat.tools.edit_image",
        "plugins.llm_chat.tools.generate_image",
    ):
        monkeypatch.setattr(importlib.import_module(name), "store_agent_attachment", audit_unavailable)

    uploads: list[list[bytes]] = []

    async def receive(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/images/edits"
        envelope = f"Content-Type: {request.headers['content-type']}\r\nMIME-Version: 1.0\r\n\r\n".encode()
        multipart = BytesParser(policy=policy.default).parsebytes(envelope + await request.aread())
        uploaded: list[bytes] = []
        for part in multipart.iter_parts():
            if part.get_param("name", header="content-disposition") == "image[]":
                data = part.get_payload(decode=True)
                assert isinstance(data, bytes)
                uploaded.append(data)
        uploads.append(uploaded)
        return httpx.Response(200, json={"created": 1, "data": [{"b64_json": base64.b64encode(_PNG).decode()}]})

    class Transport(AsyncHTTPHandler):
        def create_client(self, *_args: Any, **_kwargs: Any) -> httpx.AsyncClient:
            return httpx.AsyncClient(transport=httpx.MockTransport(receive), trust_env=False)

    transport = Transport()

    async def provider(**kwargs: Any) -> object:
        return await cast(Any, litellm.aimage_edit)(client=transport, **kwargs)

    async def forbidden_generation(**_kwargs: Any) -> object:
        pytest.fail("Reference-conditioned images must upload actual image inputs")

    model = ScopedModel(name="openai/gpt-image-2", api_key="test", base_url="https://images.example/v1", extra={})
    dispatcher = cast(Any, _Dispatcher())
    generate = cast(
        Any,
        register_generate_image(
            dispatcher,
            ImageGenerationToolContext(
                resolve_model=lambda _: model,
                generate=forbidden_generation,
                edit=provider,
                warn=lambda _: None,
                timeout_seconds=10,
                quality="high",
                output_format="png",
                output_compression=100,
            ),
        ),
    )
    edit = cast(
        Any,
        register_edit_image(
            dispatcher,
            ImageEditToolContext(
                resolve_model=lambda _: model,
                edit=provider,
                warn=lambda _: None,
                timeout_seconds=10,
                quality="high",
            ),
        ),
    )
    send = cast(Any, register_send_msg(dispatcher, SendMsgToolContext(_no_participant)))
    session = cast(Session, _Session())
    inputs = ImageInputs(attachment_root=tmp_path)
    web_pixels = _PNG + b"web-original"
    source_pixels = _PNG + b"source-original"
    web_ref = inputs.register_bytes(session, source="web", key="web", data=web_pixels)
    source_ref = inputs.register_bytes(session, source="channel", key="source", data=source_pixels)
    try:
        async with transport.client:
            for source_edit in (False, True):
                intent = MediaIntent(True, source_edit, True)
                state = DeliveryState()
                recorder = ToolTraceRecorder()
                call = recorder.start("edit_image" if source_edit else "generate_image", {})
                with (
                    image_inputs_scope(inputs),
                    llm_chat_delivery_scope(state),
                    media_intent_scope(intent) as requirements,
                    turn_resolution_scope(TurnResolution()),
                    llm_chat_tool_trace_scope(recorder),
                    llm_chat_tool_execution_scope(call.execution_ref),
                ):
                    async with prepared_media_scope():
                        if source_edit:
                            prepared = await edit(session, "Replace the subject", source_ref, [web_ref])
                        else:
                            prepared = await generate(session, "A new scene", reference_image_refs=[web_ref])
                        recorder.finish_success(call, prepared, before=DeliverySnapshot(), after=DeliverySnapshot())
                        evidence = recorder.events[0].evidence
                        assert "source_image_ref" not in evidence
                        attachments = cast(list[dict[str, Any]], evidence["attachments"])
                        expected_sources = (
                            ["channel", "web", "image_edit"] if source_edit else ["web", "image_generation"]
                        )
                        assert [item["source"] for item in attachments] == expected_sources
                        assert all(
                            item["audit_status"] == "unrecorded" and item["status"] == "ready" for item in attachments
                        )
                        assert all("image_ref" not in item and "attachment_ref" not in item for item in attachments)
                        assert not requirements.confirmed
                        assert state.confirmed_media_deliveries == 0
                        with pytest.raises(DeliveryError):
                            await finish_turn("delivered")
                        with pytest.raises(DeliveryError):
                            await send(session, [{"type": "text", "text": "Already done"}])
                        await send(session, [{"type": "media", "media_ref": prepared["media_ref"]}])
                        assert requirements.confirmed
                        assert state.confirmed_media_deliveries == 1
                        assert await finish_turn("delivered") == {"outcome": "delivered"}
                        image = cast(Any, session).sent[-1].get(Image)[0]
                        assert base64.b64decode(image.src.partition(",")[2]) == _PNG
        assert uploads == [[web_pixels], [source_pixels, web_pixels]]
    finally:
        await inputs.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unknown", "cancelled"])
async def test_unconfirmed_send_never_completes_or_reuses_resource(tmp_path: Path, failure: str) -> None:
    from plugins.llm_chat.tools.send_msg import SendMsgToolContext, register_send_msg
    from plugins.llm_chat.tools._rendering import prepare_image_bytes
    from plugins.llm_chat.tools.finish_turn import finish_turn
    from plugins.llm_chat.core.media_delivery import ImageProvenance

    session = cast(Session, _Session(failure=failure))
    send = cast(Any, register_send_msg(cast(Any, _Dispatcher()), SendMsgToolContext(_no_participant)))
    state = DeliveryState()
    intent = MediaIntent(True, False, True)
    with (
        llm_chat_delivery_scope(state),
        media_intent_scope(intent) as requirements,
        turn_resolution_scope(TurnResolution()),
    ):
        async with prepared_media_scope():
            prepared = await prepare_image_bytes(
                session,
                _PNG,
                warn=lambda _: None,
                tool_name="generate_image",
                provenance=ImageProvenance(
                    "generated", reference_image_refs=("image_reference",), reference_sources=("web",)
                ),
            )
            error = asyncio.CancelledError if failure == "cancelled" else DeliveryError
            with pytest.raises(error):
                await send(session, [{"type": "media", "media_ref": prepared["media_ref"]}])
            assert not requirements.confirmed
            assert state.delivery_attempts == 1
            assert state.confirmed_deliveries == 0
            with pytest.raises(DeliveryError):
                resolve_media(session, [cast(str, prepared["media_ref"])])
            with pytest.raises(DeliveryError):
                await finish_turn("delivered")
            assert len(cast(Any, session).sent) == 1


@pytest.mark.asyncio
async def test_web_requirement_rejects_nonweb_foreign_and_missing_references(tmp_path: Path) -> None:
    from agno.media import Image as AgnoImage

    from plugins.llm_chat.tools.edit_image import ImageEditToolContext, register_edit_image
    from plugins.llm_chat.tools.generate_image import ImageGenerationToolContext, register_generate_image
    from plugins.llm_chat.native_image_delivery import NativeImageBuffer

    async def forbidden_provider(**_kwargs: Any) -> object:
        pytest.fail("Unauthorized inputs must fail before reaching the provider")

    model = ScopedModel(name="openai/gpt-image-2", api_key="test", base_url="https://images.example/v1", extra={})
    dispatcher = cast(Any, _Dispatcher())
    generate = cast(
        Any,
        register_generate_image(
            dispatcher,
            ImageGenerationToolContext(
                resolve_model=lambda _: model,
                generate=forbidden_provider,
                edit=forbidden_provider,
                warn=lambda _: None,
                timeout_seconds=10,
                quality="high",
                output_format="png",
                output_compression=100,
            ),
        ),
    )
    edit = cast(
        Any,
        register_edit_image(
            dispatcher,
            ImageEditToolContext(
                resolve_model=lambda _: model,
                edit=forbidden_provider,
                warn=lambda _: None,
                timeout_seconds=10,
                quality="high",
            ),
        ),
    )
    session = cast(Session, _Session())
    inputs = ImageInputs(attachment_root=tmp_path / "current")
    foreign = ImageInputs(attachment_root=tmp_path / "foreign")
    direct_ref = inputs.register_bytes(session, source="direct", key="direct", data=_PNG, index=1)
    web_ref = inputs.register_bytes(session, source="web", key="web", data=_PNG)
    foreign_ref = foreign.register_bytes(session, source="web", key="foreign", data=_PNG)
    try:
        with (
            image_inputs_scope(inputs),
            llm_chat_delivery_scope(DeliveryState()),
            media_intent_scope(MediaIntent(True, False, True)),
        ):
            async with prepared_media_scope():
                with pytest.raises(DeliveryError):
                    await generate(session, "A new image")
                with pytest.raises(DeliveryError):
                    await generate(session, "A new image", reference_image_refs=[direct_ref])
                with pytest.raises(DeliveryError):
                    await generate(session, "A new image", reference_image_refs=[foreign_ref])
                with pytest.raises(DeliveryError):
                    await edit(session, "Modify this image", web_ref)
                native = NativeImageBuffer(None)
                assert native.capture(object(), (AgnoImage(content=_PNG),))
                assert native.images == []
    finally:
        await inputs.aclose()
        await foreign.aclose()
