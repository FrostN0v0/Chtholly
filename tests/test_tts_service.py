"""Unit tests for TTS providers (pure HTTP layer, no Entari runtime)."""

from types import SimpleNamespace
from typing import cast
from collections.abc import Callable

import httpx
import pytest

from utils.tts_service_core.providers.base import TTSSynthesisError
from utils.tts_service_core.providers.factory import TTSConfigLike, build_provider
from utils.tts_service_core.providers.fish_audio import FishAudioProvider
from utils.tts_service_core.providers.gpt_sovits import GptSovitsProvider

pytestmark = pytest.mark.asyncio


def make_provider(handler: Callable[[httpx.Request], httpx.Response], **kwargs) -> GptSovitsProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)
    return GptSovitsProvider("http://test/tts", client=client, **kwargs)


def make_fish_provider(handler: Callable[[httpx.Request], httpx.Response], **kwargs) -> FishAudioProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)
    return FishAudioProvider("https://fish.test/v1/tts", "fish-key", client=client, **kwargs)


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


async def test_fish_synthesize_sends_expected_headers_and_payload():
    audio = b"fake-mp3-audio"
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured["path"] = request.url.path
        captured["authorization"] = request.headers["authorization"]
        captured["content_type"] = request.headers["content-type"]
        captured["model"] = request.headers["model"]
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, content=audio, headers={"content-type": "audio/mpeg"})

    provider = make_fish_provider(
        handler,
        model="s2-pro",
        reference_id="voice-id",
        audio_format="mp3",
        latency="balanced",
        sample_rate=44100,
        prosody_speed=1.2,
        prosody_volume=-1.0,
        extra_params={"chunk_length": 150},
    )
    try:
        result = await provider.synthesize("你好", top_p=0.6)
    finally:
        await provider.close()

    assert result == audio
    assert captured["path"] == "/v1/tts"
    assert captured["authorization"] == "Bearer fish-key"
    assert captured["content_type"].startswith("application/json")
    assert captured["model"] == "s2-pro"
    assert captured["payload"]["text"] == "你好"
    assert captured["payload"]["reference_id"] == "voice-id"
    assert captured["payload"]["format"] == "mp3"
    assert captured["payload"]["latency"] == "balanced"
    assert captured["payload"]["sample_rate"] == 44100
    assert captured["payload"]["mp3_bitrate"] == 128
    assert captured["payload"]["prosody"] == {
        "speed": 1.2,
        "volume": -1.0,
        "normalize_loudness": True,
    }
    assert captured["payload"]["chunk_length"] == 150
    assert captured["payload"]["top_p"] == 0.6


async def test_fish_empty_text_short_circuits():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("should not be called")

    provider = make_fish_provider(handler)
    try:
        assert await provider.synthesize("   ") == b""
    finally:
        await provider.close()


async def test_fish_missing_api_key_raises_before_http():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("should not be called")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)
    provider = FishAudioProvider("https://fish.test/v1/tts", None, client=client)
    try:
        with pytest.raises(TTSSynthesisError, match="Fish Audio API key is required"):
            await provider.synthesize("hello")
    finally:
        await provider.close()


async def test_fish_error_status_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Invalid Token", "status": 401})

    provider = make_fish_provider(handler)
    try:
        with pytest.raises(TTSSynthesisError, match="Invalid Token"):
            await provider.synthesize("hello")
    finally:
        await provider.close()


async def test_fish_json_body_instead_of_audio_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": "not audio"})

    provider = make_fish_provider(handler)
    try:
        with pytest.raises(TTSSynthesisError, match="JSON instead of audio"):
            await provider.synthesize("hello")
    finally:
        await provider.close()


async def test_fish_timeout_raises_synthesis_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("boom")

    provider = make_fish_provider(handler)
    try:
        with pytest.raises(TTSSynthesisError, match="timed out"):
            await provider.synthesize("hello")
    finally:
        await provider.close()


async def test_fish_file_extension_matches_format():
    mp3_provider = FishAudioProvider("https://fish.test/v1/tts", "fish-key", audio_format="mp3")
    wav_provider = FishAudioProvider("https://fish.test/v1/tts", "fish-key", audio_format="wav")
    try:
        assert mp3_provider.file_extension == ".mp3"
        assert wav_provider.file_extension == ".wav"
    finally:
        await mp3_provider.close()
        await wav_provider.close()


async def test_build_provider_gpt_sovits():
    config = SimpleNamespace(
        provider="gpt-sovits",
        api_url="http://test/tts",
        timeout=15.0,
        text_lang="zh",
        default_speaker="chtholly",
        extra_params={},
    )

    assert isinstance(build_provider(cast(TTSConfigLike, config)), GptSovitsProvider)


async def test_build_provider_fish_audio():
    config = SimpleNamespace(
        provider="fish-audio",
        api_url="http://test/tts",
        timeout=15.0,
        text_lang="zh",
        default_speaker="chtholly",
        extra_params={},
        fish_api_url="https://fish.test/v1/tts",
        fish_api_key="fish-key",
        fish_model="s2-pro",
        fish_reference_id="voice-id",
        fish_format="mp3",
        fish_sample_rate=None,
        fish_mp3_bitrate=128,
        fish_latency="normal",
        fish_prosody_speed=1.0,
        fish_prosody_volume=0.0,
        fish_prosody_normalize_loudness=True,
        fish_extra_params={},
    )

    assert isinstance(build_provider(cast(TTSConfigLike, config)), FishAudioProvider)


async def test_build_provider_unknown_raises():
    config = SimpleNamespace(provider="bad")

    with pytest.raises(TTSSynthesisError, match="Unsupported TTS provider"):
        build_provider(cast(TTSConfigLike, config))
