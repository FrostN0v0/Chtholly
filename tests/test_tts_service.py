"""Unit tests for TTS providers (pure HTTP layer, no Entari runtime)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import cast
from collections.abc import Callable

import httpx
import pytest

from utils.tts_service_core.voice_catalog import TTSSynthesisRequest
from utils.tts_service_core.providers.base import TTSSynthesisError
from utils.tts_service_core.providers.types import JsonObject
from utils.tts_service_core.providers.factory import TTSConfigLike, build_provider
from utils.tts_service_core.providers.fish_audio import FishAudioProvider
from utils.tts_service_core.providers.gpt_sovits import GptSovitsProvider

pytestmark = pytest.mark.asyncio

_ZH = "\u4e2d\u6587"
_JA = "\u65e5\u8bed"
_DEFAULT = "\u9ed8\u8ba4"
_HAPPY = "\u5f00\u5fc3"
_CALM = "\u5e73\u9759"
_SPLIT = "\u6309\u6807\u70b9\u7b26\u53f7\u5207"


def make_provider(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    default_version: str = "",
    default_model: str = "",
    default_reference_language: str = "",
    default_emotion: str = "",
    inference_params: JsonObject | None = None,
) -> GptSovitsProvider:
    client = httpx.AsyncClient(
        base_url="http://test",
        transport=httpx.MockTransport(handler),
        timeout=5.0,
    )
    return GptSovitsProvider(
        "http://test",
        "secret-token",
        client=client,
        default_version=default_version,
        default_model=default_model,
        default_reference_language=default_reference_language,
        default_emotion=default_emotion,
        inference_params=inference_params,
    )


def make_fish_provider(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    model: str = "s2-pro",
    reference_id: str | None = None,
    audio_format: str = "mp3",
    sample_rate: int | None = None,
    latency: str = "normal",
    prosody_speed: float = 1.0,
    prosody_volume: float = 0.0,
    extra_params: JsonObject | None = None,
) -> FishAudioProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)
    return FishAudioProvider(
        "https://fish.test/v1/tts",
        "fish-key",
        client=client,
        model=model,
        reference_id=reference_id,
        audio_format=audio_format,
        sample_rate=sample_rate,
        latency=latency,
        prosody_speed=prosody_speed,
        prosody_volume=prosody_volume,
        extra_params=extra_params,
    )


def catalog_response(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/version":
        return httpx.Response(200, json={"support_versions": ["v4"]})
    if request.url.path == "/models/v4":
        return httpx.Response(
            200,
            json={
                "models": {
                    "Chtholly": {
                        _ZH: [_DEFAULT, _HAPPY],
                        _JA: [_CALM],
                    },
                    "Nephren": {_ZH: [_DEFAULT]},
                }
            },
        )
    raise AssertionError(f"unexpected request: {request.method} {request.url.path}")


async def test_gpt_catalog_discovers_live_voice_hierarchy_and_caches_it():
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer secret-token"
        requests.append(request.url.path)
        return catalog_response(request)

    provider = make_provider(
        handler,
        default_version="v4",
        default_model="Chtholly",
        default_reference_language=_ZH,
        default_emotion=_HAPPY,
    )
    try:
        catalog = await provider.get_voice_catalog()
        cached = await provider.get_voice_catalog()
    finally:
        await provider.close()

    assert cached is catalog
    assert requests == ["/version", "/models/v4"]
    assert catalog.provider == "gpt-sovits"
    assert catalog.supports_inline_style_tags is False
    assert catalog.text_languages[0] == _ZH
    assert [voice.model_name for voice in catalog.voices] == ["Chtholly", "Nephren"]
    assert catalog.voices[0].references[0].language == _ZH
    assert catalog.voices[0].references[0].emotions == (_DEFAULT, _HAPPY)
    assert catalog.default_selection is not None
    assert catalog.default_selection.model_name == "Chtholly"
    assert catalog.default_selection.emotion == _HAPPY


async def test_gpt_synthesize_uses_exact_selected_voice_and_semantic_options():
    audio = b"RIFF....WAVEfmt fake-audio-data"
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in {"/version", "/models/v4"}:
            return catalog_response(request)
        if request.url.path == "/v1/audio/speech":
            captured["authorization"] = request.headers["authorization"]
            captured["payload"] = json.loads(request.content)
            return httpx.Response(200, content=audio, headers={"content-type": "audio/wav"})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    provider = make_provider(handler, inference_params={"top_k": 5, "text_split_method": _SPLIT})
    request = TTSSynthesisRequest(
        text="Hello",
        version="v4",
        model_name="Chtholly",
        reference_language=_JA,
        emotion=_CALM,
        text_language=_ZH,
        speed=1.15,
    )
    try:
        result = await provider.synthesize(request)
    finally:
        await provider.close()

    payload = cast(dict[str, object], captured["payload"])
    other_params = cast(dict[str, object], payload["other_params"])
    assert result == audio
    assert provider.file_extension == ".wav"
    assert captured["authorization"] == "Bearer secret-token"
    assert payload == {
        "model": "GSVI-v4",
        "input": "Hello",
        "voice": "Chtholly",
        "response_format": "wav",
        "speed": 1.15,
        "other_params": other_params,
    }
    assert other_params == {
        "top_k": 5,
        "text_split_method": _SPLIT,
        "text_lang": _ZH,
        "prompt_lang": _JA,
        "emotion": _CALM,
    }


async def test_gpt_rejects_unknown_character_before_synthesis():
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        return catalog_response(request)

    provider = make_provider(handler)
    try:
        with pytest.raises(TTSSynthesisError, match="Unsupported model"):
            await provider.synthesize(TTSSynthesisRequest(text="Hello", model_name="Missing"))
    finally:
        await provider.close()

    assert requested_paths == ["/version", "/models/v4"]


async def test_gpt_empty_text_short_circuits_before_catalog_request():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("should not be called")

    provider = make_provider(handler)
    try:
        assert await provider.synthesize(TTSSynthesisRequest(text="   ")) == b""
    finally:
        await provider.close()


async def test_gpt_catalog_error_status_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "Unauthorized"})

    provider = make_provider(handler)
    try:
        with pytest.raises(TTSSynthesisError, match="401"):
            await provider.get_voice_catalog()
    finally:
        await provider.close()


async def test_gpt_json_body_instead_of_audio_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path in {"/version", "/models/v4"}:
            return catalog_response(request)
        return httpx.Response(200, json={"message": "not audio"})

    provider = make_provider(handler)
    try:
        with pytest.raises(TTSSynthesisError, match="JSON instead of audio"):
            await provider.synthesize(TTSSynthesisRequest(text="Hello"))
    finally:
        await provider.close()


async def test_gpt_timeout_raises_synthesis_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("boom")

    provider = make_provider(handler)
    try:
        with pytest.raises(TTSSynthesisError, match="timed out"):
            await provider.get_voice_catalog()
    finally:
        await provider.close()


async def test_fish_synthesize_sends_expected_headers_and_payload():
    audio = b"fake-mp3-audio"
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
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
        extra_params={"chunk_length": 150, "top_p": 0.6},
    )
    try:
        result = await provider.synthesize(TTSSynthesisRequest(text="Hello", speed=1.4))
        catalog = await provider.get_voice_catalog()
    finally:
        await provider.close()

    payload = cast(dict[str, object], captured["payload"])
    assert result == audio
    assert captured["path"] == "/v1/tts"
    assert captured["authorization"] == "Bearer fish-key"
    assert str(captured["content_type"]).startswith("application/json")
    assert captured["model"] == "s2-pro"
    assert payload["text"] == "Hello"
    assert payload["reference_id"] == "voice-id"
    assert payload["format"] == "mp3"
    assert payload["latency"] == "balanced"
    assert payload["sample_rate"] == 44100
    assert payload["prosody"] == {
        "speed": 1.4,
        "volume": -1.0,
        "normalize_loudness": True,
    }
    assert payload["chunk_length"] == 150
    assert payload["top_p"] == 0.6
    assert catalog.voices == ()
    assert catalog.supports_inline_style_tags is True


async def test_fish_empty_text_short_circuits():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("should not be called")

    provider = make_fish_provider(handler)
    try:
        assert await provider.synthesize(TTSSynthesisRequest(text="   ")) == b""
    finally:
        await provider.close()


async def test_fish_missing_api_key_raises_before_http():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("should not be called")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)
    provider = FishAudioProvider("https://fish.test/v1/tts", None, client=client)
    try:
        with pytest.raises(TTSSynthesisError, match="Fish Audio API key is required"):
            await provider.synthesize(TTSSynthesisRequest(text="Hello"))
    finally:
        await provider.close()


async def test_fish_rejects_gpt_voice_selectors():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("should not be called")

    provider = make_fish_provider(handler)
    try:
        with pytest.raises(TTSSynthesisError, match="does not support GPT-SoVITS voice selectors"):
            await provider.synthesize(TTSSynthesisRequest(text="Hello", model_name="Chtholly"))
    finally:
        await provider.close()


async def test_fish_error_status_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Invalid Token", "status": 401})

    provider = make_fish_provider(handler)
    try:
        with pytest.raises(TTSSynthesisError, match="Invalid Token"):
            await provider.synthesize(TTSSynthesisRequest(text="Hello"))
    finally:
        await provider.close()


async def test_fish_json_body_instead_of_audio_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": "not audio"})

    provider = make_fish_provider(handler)
    try:
        with pytest.raises(TTSSynthesisError, match="JSON instead of audio"):
            await provider.synthesize(TTSSynthesisRequest(text="Hello"))
    finally:
        await provider.close()


async def test_fish_timeout_raises_synthesis_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("boom")

    provider = make_fish_provider(handler)
    try:
        with pytest.raises(TTSSynthesisError, match="timed out"):
            await provider.synthesize(TTSSynthesisRequest(text="Hello"))
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


def provider_config(provider: str) -> SimpleNamespace:
    return SimpleNamespace(
        provider=provider,
        timeout=300.0,
        gpt_sovits_base_url="http://test",
        gpt_sovits_api_key="secret-token",
        gpt_sovits_default_version="v4",
        gpt_sovits_default_model="Chtholly",
        gpt_sovits_default_reference_language=_ZH,
        gpt_sovits_default_emotion=_DEFAULT,
        gpt_sovits_text_language=_ZH,
        gpt_sovits_speed=1.0,
        gpt_sovits_format="wav",
        gpt_sovits_catalog_ttl=300.0,
        gpt_sovits_extra_params={},
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


async def test_build_provider_gpt_sovits():
    config = provider_config("gpt-sovits")
    provider = build_provider(cast(TTSConfigLike, config))
    try:
        assert isinstance(provider, GptSovitsProvider)
    finally:
        await provider.close()


async def test_build_provider_fish_audio():
    config = provider_config("fish-audio")
    provider = build_provider(cast(TTSConfigLike, config))
    try:
        assert isinstance(provider, FishAudioProvider)
    finally:
        await provider.close()


async def test_build_provider_unknown_raises():
    config = SimpleNamespace(provider="bad")

    with pytest.raises(TTSSynthesisError, match="Unsupported TTS provider"):
        build_provider(cast(TTSConfigLike, config))
