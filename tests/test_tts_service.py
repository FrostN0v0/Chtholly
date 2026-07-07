"""Unit tests for the gpt-sovits TTS provider (pure HTTP layer, no Entari runtime)."""

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugins" / "tts_service"))

from providers.base import TTSSynthesisError  # noqa: E402
from providers.gpt_sovits import GptSovitsProvider  # noqa: E402

pytestmark = pytest.mark.asyncio


def make_provider(handler, **kwargs) -> GptSovitsProvider:
    provider = GptSovitsProvider("http://test/tts", **kwargs)
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)
    return provider


async def test_synthesize_returns_audio_bytes():
    audio = b"RIFF....WAVEfmt fake-audio-data"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=audio, headers={"content-type": "audio/wav"})

    provider = make_provider(handler)
    try:
        result = await provider.synthesize("测试文本")
        assert result == audio
    finally:
        await provider.close()


async def test_empty_text_short_circuits():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("should not be called")

    provider = make_provider(handler)
    try:
        assert await provider.synthesize("   ") == b""
    finally:
        await provider.close()


async def test_payload_contains_text_lang_and_extra_params():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured.update(json.loads(request.content))
        return httpx.Response(200, content=b"x", headers={"content-type": "audio/wav"})

    provider = make_provider(
        handler,
        text_lang="zh",
        default_speaker="chtholly",
        extra_params={"ref_audio_path": "ref.wav", "prompt_lang": "zh"},
    )
    try:
        await provider.synthesize("你好", top_k=5)
    finally:
        await provider.close()

    assert captured["text"] == "你好"
    assert captured["text_lang"] == "zh"
    assert captured["speaker"] == "chtholly"
    assert captured["ref_audio_path"] == "ref.wav"
    assert captured["top_k"] == 5


async def test_error_status_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "bad ref audio"})

    provider = make_provider(handler)
    try:
        with pytest.raises(TTSSynthesisError, match="400"):
            await provider.synthesize("你好")
    finally:
        await provider.close()


async def test_json_body_instead_of_audio_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": "streaming not supported"})

    provider = make_provider(handler)
    try:
        with pytest.raises(TTSSynthesisError, match="JSON instead of audio"):
            await provider.synthesize("你好")
    finally:
        await provider.close()


async def test_timeout_raises_synthesis_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("boom")

    provider = make_provider(handler)
    try:
        with pytest.raises(TTSSynthesisError, match="timed out"):
            await provider.synthesize("你好")
    finally:
        await provider.close()
