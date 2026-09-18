"""Behavioral coverage for inventory discovery independent of stale TTS defaults."""

from __future__ import annotations

import json
from typing import cast
from pathlib import Path
from dataclasses import replace
from importlib.util import module_from_spec, spec_from_file_location
from collections.abc import Callable

import httpx
import pytest

from utils.tts_service_core.voice_catalog import TTSVoiceCatalog, TTSSynthesisRequest
from utils.tts_service_core.providers.base import TTSSynthesisError
from utils.tts_service_core.providers.fish_audio import FishAudioProvider
from utils.tts_service_core.providers.gpt_sovits import GptSovitsProvider

pytestmark = pytest.mark.asyncio

_ZH = "中文"
_DEFAULT = "默认"
_AUDIO = b"RIFF....WAVEfmt selected-live-voice"


@pytest.fixture
def serialize_catalog() -> Callable[[TTSVoiceCatalog], str]:
    path = Path(__file__).resolve().parents[1] / "plugins" / "llm_chat" / "tools" / "_tts.py"
    spec = spec_from_file_location("_isolated_tts_catalog_serializer", path)
    assert spec is not None
    assert spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return cast(Callable[[TTSVoiceCatalog], str], module.serialize_voice_catalog)


async def test_removed_default_preserves_refreshed_inventory_without_leaking_configuration(
    serialize_catalog: Callable[[TTSVoiceCatalog], str],
):
    retired = "private/default/removed-character"
    models = {retired: {_ZH: [_DEFAULT]}, "Live": {_ZH: [_DEFAULT]}}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/version":
            return httpx.Response(200, json={"support_versions": ["v4"]})
        if request.url.path == "/models/v4":
            return httpx.Response(200, json={"models": models})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    client = httpx.AsyncClient(base_url="http://tts.test", transport=httpx.MockTransport(handler))
    provider = GptSovitsProvider("http://tts.test", None, client=client, default_model=retired)
    try:
        initial = await provider.get_voice_catalog()
        assert initial.default_selection is not None
        del models[retired]
        assert await provider.get_voice_catalog() is initial
        current = await provider.get_voice_catalog(refresh=True)
        assert await provider.get_voice_catalog() is current
        serialized = serialize_catalog(current)
    finally:
        await provider.close()

    payload = json.loads(serialized)
    assert payload["voice_selection_available"] is True
    assert [voice["model_name"] for voice in payload["voices"]] == ["Live"]
    assert payload["voices"][0]["references"] == [{"language": _ZH, "emotions": [_DEFAULT]}]
    assert payload["default_selection"] is None
    assert payload["default_selection_error"]["code"] == "invalid_default_selection"
    assert payload["default_selection_error"]["field"] == "model_name"
    assert retired not in serialized


@pytest.mark.parametrize(
    "invalid_field", ["version", "model_name", "reference_language", "emotion", "text_language", "speed"]
)
async def test_synthesis_validates_actual_selection_not_unrelated_stale_defaults(
    invalid_field: str,
    serialize_catalog: Callable[[TTSVoiceCatalog], str],
):
    speech_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal speech_requests
        if request.url.path == "/version":
            return httpx.Response(200, json={"support_versions": ["v4"]})
        if request.url.path == "/models/v4":
            return httpx.Response(200, json={"models": {"Live": {_ZH: [_DEFAULT]}}})
        if request.url.path == "/v1/audio/speech":
            speech_requests += 1
            payload = json.loads(request.content)
            options = payload["other_params"]
            # The transport accepts only this real inventory selection, not a fallback voice.
            if (
                payload["model"] == "GSVI-v4"
                and payload["voice"] == "Live"
                and payload["speed"] == 1.2
                and options["prompt_lang"] == _ZH
                and options["emotion"] == _DEFAULT
                and options["text_lang"] == _ZH
            ):
                return httpx.Response(200, content=_AUDIO, headers={"content-type": "audio/wav"})
            return httpx.Response(422, json={"error": "selection does not match installed voice"})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    stale = "private-stale-configuration"
    client = httpx.AsyncClient(base_url="http://tts.test", transport=httpx.MockTransport(handler))
    provider = GptSovitsProvider(
        "http://tts.test",
        None,
        client=client,
        default_version=stale if invalid_field == "version" else "v4",
        default_model=stale if invalid_field == "model_name" else "Live",
        default_reference_language=stale if invalid_field == "reference_language" else _ZH,
        default_emotion=stale if invalid_field == "emotion" else _DEFAULT,
        default_text_language=stale if invalid_field == "text_language" else _ZH,
        default_speed=99.0 if invalid_field == "speed" else 1.2,
    )
    selection = TTSSynthesisRequest(
        text="Hello",
        version="v4",
        model_name="Live",
        reference_language=_ZH,
        emotion=_DEFAULT,
        text_language=_ZH,
        speed=1.2,
    )
    try:
        catalog = await provider.get_voice_catalog()
        serialized = serialize_catalog(catalog)
        payload = json.loads(serialized)
        assert payload["default_selection"] is None
        assert payload["default_selection_error"]["field"] == invalid_field
        assert stale not in serialized
        assert await provider.synthesize(selection) == _AUDIO
        with pytest.raises(TTSSynthesisError, match="speed" if invalid_field == "speed" else "Unsupported") as omitted:
            await provider.synthesize(replace(selection, **{invalid_field: None}))
        if invalid_field != "speed":
            assert stale in str(omitted.value)
        with pytest.raises(TTSSynthesisError, match="Unsupported model"):
            await provider.synthesize(replace(selection, model_name="ExplicitlyMissing"))
        assert speech_requests == 1
    finally:
        await provider.close()


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/version", {"support_versions": "v4"}),
        ("/version", {"support_versions": ["v4", None]}),
        ("/models/v4", {"models": []}),
        ("/models/v4", {"models": {"Live": {_ZH: "not-an-emotion-array"}}}),
    ],
)
async def test_malformed_inventory_is_not_reported_as_stale_default(path: str, payload: object):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == path:
            return httpx.Response(200, json=payload)
        return httpx.Response(200, json={"support_versions": ["v4"]})

    client = httpx.AsyncClient(base_url="http://tts.test", transport=httpx.MockTransport(handler))
    provider = GptSovitsProvider("http://tts.test", None, client=client, default_model="Missing")
    try:
        with pytest.raises(TTSSynthesisError, match="catalog"):
            await provider.get_voice_catalog()
    finally:
        await provider.close()


async def test_failed_refresh_is_not_misrepresented_as_default_diagnostic():
    fail_refresh = False

    def handler(request: httpx.Request) -> httpx.Response:
        if fail_refresh:
            return httpx.Response(503, json={"error": "unavailable"})
        if request.url.path == "/version":
            return httpx.Response(200, json={"support_versions": ["v4"]})
        return httpx.Response(200, json={"models": {"Live": {_ZH: [_DEFAULT]}}})

    client = httpx.AsyncClient(base_url="http://tts.test", transport=httpx.MockTransport(handler))
    provider = GptSovitsProvider("http://tts.test", None, client=client, default_model="Missing")
    try:
        cached = await provider.get_voice_catalog()
        fail_refresh = True
        with pytest.raises(TTSSynthesisError, match="503"):
            await provider.get_voice_catalog(refresh=True)
        assert await provider.get_voice_catalog() is cached
    finally:
        await provider.close()


async def test_fish_absent_voice_selection_is_not_an_invalid_default(
    serialize_catalog: Callable[[TTSVoiceCatalog], str],
):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Fish capability discovery must not fetch a GPT-SoVITS inventory")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = FishAudioProvider("https://fish.test/v1/tts", "test-key", client=client)
    try:
        payload = json.loads(serialize_catalog(await provider.get_voice_catalog()))
    finally:
        await provider.close()
    assert payload["voice_selection_available"] is False
    assert payload["supports_inline_style_tags"] is True
    assert payload["default_selection"] is None
    assert payload["default_selection_error"] is None
