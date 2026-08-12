"""Behavioral tests for native model image output and delivery."""

from __future__ import annotations

from types import SimpleNamespace
import base64
from typing import Any, cast
from pathlib import Path

import pytest
from agno.media import Image as AgnoImage
from arclet.entari import Session
from arclet.entari.config import EntariConfig

if not hasattr(EntariConfig, "instance"):
    setattr(EntariConfig, "instance", EntariConfig.load(Path(__file__).resolve().parents[1] / "entari.yml"))

from plugins.llm_chat import generation, agno_compat
from plugins.llm_chat.core.media import sanitize_assistant_history, strip_internal_media_records
from plugins.llm_chat.core.delivery import DeliveryError, DeliveryState
from plugins.llm_chat.turn_lifecycle import ActiveChatTurn
from plugins.llm_chat.core.image_source import IMAGE_FETCH_MAX_BYTES
from plugins.llm_chat.core.native_images import extract_native_images

_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)
_DATA_URL = f"data:image/png;base64,{base64.b64encode(_PNG_BYTES).decode('ascii')}"


class _Session:
    def __init__(self, *, fail_at: int | None = None) -> None:
        self.sent: list[Any] = []
        self.fail_at = fail_at

    async def send(self, payload: Any) -> None:
        if self.fail_at is not None and len(self.sent) + 1 == self.fail_at:
            raise RuntimeError("transport failed")
        self.sent.append(payload)


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


def test_agno_compat_litellm_wrapper_attaches_provider_images() -> None:
    class _LiteLLM:
        def _parse_provider_response(self, _response: object, **_kwargs: Any) -> SimpleNamespace:
            return SimpleNamespace(images=None)

    wrapped = agno_compat._wrap_litellm_model(_LiteLLM)
    provider_response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=None, images=[{"image_url": {"url": _DATA_URL}}]))]
    )

    parsed = wrapped()._parse_provider_response(provider_response)

    assert parsed.images is not None
    assert parsed.images[0].content == _PNG_BYTES


@pytest.mark.asyncio
async def test_image_only_generation_does_not_run_tool_free_finalizer(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def generate(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        nonlocal calls
        calls += 1
        return SimpleNamespace(content=None, images=[AgnoImage(content=_PNG_BYTES)])

    async def unexpected_finalizer(**_kwargs: Any) -> None:
        raise AssertionError("image-only output must not trigger finalization")

    monkeypatch.setattr(generation, "llm", SimpleNamespace(generate=generate))
    monkeypatch.setattr(generation.litellm, "acompletion", unexpected_finalizer)

    response = await generation.generate_chat_response(
        [{"role": "user", "content": "hello"}],
        system="system",
        model="model",
        channel_id="group",
        ctx=None,
        web_limits=generation.WebAccessLimits(0, 0, 0),
        delivery_state=DeliveryState(),
    )

    assert calls == 1
    assert extract_native_images(response)


@pytest.mark.asyncio
async def test_native_image_satisfies_explicit_media_request_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[dict[str, Any]] = []

    async def generate(*_args: Any, **kwargs: Any) -> SimpleNamespace:
        requests.append(kwargs)
        return SimpleNamespace(content=None, images=[AgnoImage(content=_PNG_BYTES)])

    async def unexpected_finalizer(**_kwargs: Any) -> None:
        raise AssertionError("native image output must not trigger media recovery")

    monkeypatch.setattr(generation, "llm", SimpleNamespace(generate=generate))
    monkeypatch.setattr(generation.litellm, "acompletion", unexpected_finalizer)

    await generation.generate_chat_response(
        [{"role": "user", "content": "来张图我看看"}],
        system="system",
        model="model",
        channel_id="group",
        ctx=None,
        web_limits=generation.WebAccessLimits(0, 0, 0),
        delivery_state=DeliveryState(),
        request_timeout=12.5,
        media_request_timeout=45.0,
    )

    assert len(requests) == 1
    assert requests[0]["timeout"] == 45.0
    assert requests[0]["max_retries"] == 0


@pytest.mark.asyncio
async def test_native_images_are_sent_before_text_and_marked_individually() -> None:
    session = _Session()
    history: list[str] = []
    state = DeliveryState()
    turn = ActiveChatTurn(
        channel_id="group",
        user_message_id=1,
        delivery_state=state,
        append_history=lambda _channel, _user, _name, _role, content: _append(history, content),
        delete_history=lambda _message_id: _noop(),
        warn=lambda _message: None,
    )
    response = SimpleNamespace(_run_output=SimpleNamespace(images=[AgnoImage(content=_PNG_BYTES)]))

    assert await turn.deliver_model_images(cast(Session, session), response)
    assert await turn.deliver_model_reply(cast(Session, session), "final text")
    await turn.persist_delivered_text()

    assert len(session.sent) == 2
    assert history == ["[发送了图片]", "final text"]
    assert state.confirmed_media_deliveries == 1


@pytest.mark.asyncio
async def test_native_image_transport_failure_keeps_confirmed_prefix() -> None:
    session = _Session(fail_at=2)
    history: list[str] = []
    state = DeliveryState()
    turn = ActiveChatTurn(
        channel_id="group",
        user_message_id=1,
        delivery_state=state,
        append_history=lambda _channel, _user, _name, _role, content: _append(history, content),
        delete_history=lambda _message_id: _noop(),
        warn=lambda _message: None,
    )
    response = SimpleNamespace(
        _run_output=SimpleNamespace(
            images=[AgnoImage(content=_PNG_BYTES), AgnoImage(content=_PNG_BYTES)],
        )
    )

    with pytest.raises(DeliveryError, match="native image delivery confirmed 1/2 images before failure"):
        await turn.deliver_model_images(cast(Session, session), response)

    assert len(session.sent) == 1
    assert history == ["[发送了图片]"]
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
