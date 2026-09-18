"""Behavioral tests for native image preparation and explicit message delivery."""

from __future__ import annotations

from types import SimpleNamespace
import base64
from typing import Any, cast
from pathlib import Path

import pytest
from agno.media import Image as AgnoImage
from satori.model import MessageObject
from arclet.entari import Text, Image, Session, MessageChain
from arclet.entari.config import EntariConfig

if not hasattr(EntariConfig, "instance"):
    setattr(EntariConfig, "instance", EntariConfig.load(Path(__file__).resolve().parents[1] / "entari.yml"))

from plugins.llm_chat import generation
from plugins.llm_chat.core.media import sanitize_assistant_history, strip_internal_media_records
from plugins.llm_chat.agent_context import AgentAccessContext
from plugins.llm_chat.core.delivery import DeliveryError, DeliveryState, llm_chat_delivery_scope
from plugins.llm_chat.prepared_media import list_prepared_media, prepared_media_scope
from plugins.llm_chat.tools.send_msg import SendMsgToolContext, register_send_msg
from plugins.llm_chat.turn_lifecycle import ActiveChatTurn
from plugins.llm_chat.core.image_source import IMAGE_FETCH_MAX_BYTES
from plugins.llm_chat.core.native_images import extract_native_images
from plugins.llm_chat.native_image_delivery import native_image_delivery_scope

_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)
_DATA_URL = f"data:image/png;base64,{base64.b64encode(_PNG_BYTES).decode('ascii')}"


class _Session:
    def __init__(self, *, fail_at: int | None = None) -> None:
        self.account = SimpleNamespace(platform="test", self_id="bot")
        self.channel = SimpleNamespace(id="group")
        self.user = SimpleNamespace(id="user", name="User")
        self.sent: list[Any] = []
        self.fail_at = fail_at

    async def send(self, payload: Any) -> list[MessageObject]:
        if self.fail_at is not None and len(self.sent) + 1 == self.fail_at:
            raise RuntimeError("transport failed")
        self.sent.append(payload)
        return [MessageObject(id=f"sent-{len(self.sent)}", content=str(payload))]


@pytest.mark.parametrize(
    "response",
    [
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=None,
                        images=[{"type": "image_url", "image_url": {"url": _DATA_URL}}],
                    )
                )
            ]
        ),
        SimpleNamespace(_run_output=SimpleNamespace(images=[AgnoImage(content=_PNG_BYTES)])),
    ],
)
def test_native_image_sources_survive_provider_and_agno_boundaries(response: object) -> None:
    images = extract_native_images(response)

    assert len(images) == 1
    assert images[0].mime_type == "image/png"
    assert images[0].content == _PNG_BYTES


class _ToolDispatcher:
    plugin = SimpleNamespace(module=SimpleNamespace(__name__=__name__))

    def __call__(self, function: Any) -> Any:
        return function


async def _no_participant(_session: Session, _reference: str) -> None:
    return None


_send_msg = cast(Any, register_send_msg(cast(Any, _ToolDispatcher()), SendMsgToolContext(_no_participant)))


def _turn(state: DeliveryState, history: list[str]) -> ActiveChatTurn:
    return ActiveChatTurn(
        channel_id="group",
        user_message_id=1,
        delivery_state=state,
        append_history=lambda _channel, _user, _name, _role, content: _append(history, content),
        delete_history=lambda _message_id: _noop(),
        warn=lambda _message: None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("user_text", ["hello", "来张图我看看"])
async def test_native_output_requires_tool_enabled_confirmation(
    monkeypatch: pytest.MonkeyPatch, user_text: str
) -> None:
    session = _Session()
    state = DeliveryState()
    history: list[str] = []
    turn = _turn(state, history)
    requests: list[dict[str, Any]] = []

    async def generate(messages: Any, **kwargs: Any) -> SimpleNamespace:
        requests.append(kwargs)
        if len(requests) == 1:
            return SimpleNamespace(content=None, images=[AgnoImage(content=_PNG_BYTES)])
        prepared = list_prepared_media()
        assert len(prepared) == 1
        assert prepared[0]["media_ref"] in str(messages) + str(kwargs)
        assert session.sent == []
        assert history == []
        assert state.delivery_attempts == state.confirmed_media_deliveries == 0
        await _send_msg(cast(Session, session), [{"type": "media", "media_ref": prepared[0]["media_ref"]}])
        return SimpleNamespace(content="[END_OF_RESPONSE]")

    async def unexpected_finalizer(**_kwargs: Any) -> None:
        raise AssertionError("Native media confirmation requires tool access")

    monkeypatch.setattr(generation, "llm", SimpleNamespace(generate=generate))
    monkeypatch.setattr(generation.litellm, "acompletion", unexpected_finalizer)
    with native_image_delivery_scope(lambda response: turn.prepare_model_images(cast(Session, session), response)):
        response = await generation.generate_chat_response(
            [{"role": "user", "content": user_text}],
            system="system",
            model="model",
            channel_id="group",
            ctx=None,
            web_limits=generation.WebAccessLimits(0, 0, 0),
            delivery_state=state,
            agent_access=AgentAccessContext(1, 1, 1, "user", raw_user_text=user_text),
            request_timeout=12.5,
            media_request_timeout=45.0,
        )
    await turn.persist_delivered_text()

    assert len(requests) == 2
    assert requests[1]["parallel_tool_calls"] is False
    assert all(item["timeout"] == (12.5 if user_text == "hello" else 45.0) for item in requests)
    if user_text != "hello":
        assert all(item["max_retries"] == 0 for item in requests)
    assert generation.response_content(response) == "[END_OF_RESPONSE]"
    assert len(session.sent) == 1
    assert state.confirmed_media_deliveries == 1
    assert history == ["[发送了图片]"]


@pytest.mark.asyncio
async def test_prepared_native_image_follows_model_order_in_one_history_row() -> None:
    session = _Session()
    history: list[str] = []
    state = DeliveryState()
    turn = _turn(state, history)
    response = SimpleNamespace(_run_output=SimpleNamespace(images=[AgnoImage(content=_PNG_BYTES)]))

    with llm_chat_delivery_scope(state):
        async with prepared_media_scope():
            assert await turn.prepare_model_images(cast(Session, session), response)
            prepared = list_prepared_media()
            assert session.sent == history == state.delivered_texts == []
            assert state.delivery_attempts == state.confirmed_media_deliveries == state.media_messages == 0
            await _send_msg(
                cast(Session, session),
                [
                    {"type": "text", "text": "before"},
                    {"type": "media", "media_ref": prepared[0]["media_ref"]},
                    {"type": "text", "text": "after"},
                ],
            )
    await turn.persist_delivered_text()
    await turn.persist_delivered_text()

    assert len(session.sent) == 1
    assert isinstance(session.sent[0], MessageChain)
    assert [type(element) for element in session.sent[0]] == [Text, Image, Text]
    assert history == ["before[发送了图片]after"]
    assert state.confirmed_media_deliveries == 1


@pytest.mark.asyncio
async def test_native_image_unknown_transport_preserves_only_confirmed_chain() -> None:
    session = _Session(fail_at=2)
    history: list[str] = []
    state = DeliveryState()
    turn = _turn(state, history)
    response = SimpleNamespace(images=[AgnoImage(content=_PNG_BYTES), AgnoImage(content=_PNG_BYTES)])

    with llm_chat_delivery_scope(state):
        async with prepared_media_scope():
            assert await turn.prepare_model_images(cast(Session, session), response)
            first, second = list_prepared_media()
            await _send_msg(cast(Session, session), [{"type": "media", "media_ref": first["media_ref"]}])
            with pytest.raises(DeliveryError):
                await _send_msg(cast(Session, session), [{"type": "media", "media_ref": second["media_ref"]}])
            with pytest.raises(DeliveryError):
                await _send_msg(cast(Session, session), [{"type": "media", "media_ref": second["media_ref"]}])
    await turn.persist_delivered_text()

    assert len(session.sent) == 1
    assert history == ["[发送了图片]"]
    assert state.delivery_attempts == 2
    assert state.confirmed_media_deliveries == 1


def test_invalid_oversized_and_local_native_sources_are_not_successful() -> None:
    oversized = "data:image/png;base64," + "A" * (((IMAGE_FETCH_MAX_BYTES + 2) // 3) * 4 + 1)
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=None,
                    images=[
                        {"image_url": {"url": "C:\\private\\image.png"}},
                        {"image_url": {"url": oversized}},
                        {"image_url": {"url": "data:image/png;base64,AAAA"}},
                    ],
                )
            )
        ]
    )

    assert extract_native_images(response) == ()


def test_native_image_marker_is_internal_history_only() -> None:
    assert sanitize_assistant_history("[发送了图片]") is None
    assert strip_internal_media_records("[发送了图片]可见文字") == "可见文字"


async def _append(history: list[str], content: str) -> None:
    history.append(content)


async def _noop() -> None:
    return None
