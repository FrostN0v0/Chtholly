"""GPT-SoVITS GSVI-compatible provider with dynamic voice discovery."""

from __future__ import annotations

import time
import asyncio
from urllib.parse import quote
from collections.abc import Mapping, Callable

import httpx

from .base import TTSSynthesisError
from .types import JsonObject
from .http_utils import response_detail, map_request_error, ensure_audio_response
from ..voice_catalog import TTSVoiceCatalog, TTSSynthesisRequest
from .gpt_sovits_catalog import (
    DEFAULT_SPLIT_METHOD,
    DEFAULT_TEXT_LANGUAGE,
    GPT_SOVITS_AUDIO_FORMATS,
    parse_voices,
    string_items,
    build_catalog,
    resolve_selection,
)


class GptSovitsProvider:
    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        *,
        timeout: float = 300.0,
        default_version: str = "",
        default_model: str = "",
        default_reference_language: str = "",
        default_emotion: str = "",
        default_text_language: str = DEFAULT_TEXT_LANGUAGE,
        default_speed: float = 1.0,
        audio_format: str = "wav",
        catalog_ttl: float = 300.0,
        inference_params: JsonObject | None = None,
        client: httpx.AsyncClient | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        normalized_base_url = base_url.rstrip("/")
        if not normalized_base_url:
            raise TTSSynthesisError("GPT-SoVITS base URL is required")
        if audio_format not in GPT_SOVITS_AUDIO_FORMATS:
            raise TTSSynthesisError(f"Unsupported GPT-SoVITS audio format: {audio_format}")

        self.api_key = api_key.strip() if api_key else ""
        self.default_version = default_version.strip()
        self.default_model = default_model.strip()
        self.default_reference_language = default_reference_language.strip()
        self.default_emotion = default_emotion.strip()
        self.default_text_language = default_text_language.strip() or DEFAULT_TEXT_LANGUAGE
        self.default_speed = default_speed
        self.audio_format = audio_format
        self.catalog_ttl = max(0.0, catalog_ttl)
        self.inference_params = dict(inference_params or {})
        self._client = client or httpx.AsyncClient(base_url=normalized_base_url, timeout=timeout)
        self._monotonic = monotonic
        self._catalog: TTSVoiceCatalog | None = None
        self._catalog_expires_at = 0.0
        self._catalog_lock = asyncio.Lock()

    @property
    def file_extension(self) -> str:
        return f".{self.audio_format}"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    async def _get_json(self, path: str) -> dict[str, object]:
        try:
            response = await self._client.get(path, headers=self._headers())
        except httpx.HTTPError as exc:
            raise map_request_error(exc) from exc
        if response.status_code != 200:
            raise TTSSynthesisError(
                f"GPT-SoVITS catalog request returned {response.status_code}: {response_detail(response)}"
            )
        try:
            payload: object = response.json()
        except ValueError as exc:
            raise TTSSynthesisError("GPT-SoVITS catalog returned invalid JSON") from exc
        if not isinstance(payload, Mapping):
            raise TTSSynthesisError("GPT-SoVITS catalog returned a non-object payload")
        return {key: value for key, value in payload.items() if isinstance(key, str)}

    async def _fetch_voice_catalog(self) -> TTSVoiceCatalog:
        version_payload = await self._get_json("/version")
        versions = string_items(version_payload.get("support_versions"))
        voices = []
        for version in versions:
            payload = await self._get_json(f"/models/{quote(version, safe='')}")
            voices.extend(parse_voices(version, payload.get("models")))
        return build_catalog(
            voices,
            default_version=self.default_version,
            default_model=self.default_model,
            default_reference_language=self.default_reference_language,
            default_emotion=self.default_emotion,
            default_text_language=self.default_text_language,
            default_speed=self.default_speed,
        )

    async def get_voice_catalog(self, *, refresh: bool = False) -> TTSVoiceCatalog:
        now = self._monotonic()
        if not refresh and self._catalog is not None and now < self._catalog_expires_at:
            return self._catalog
        async with self._catalog_lock:
            now = self._monotonic()
            if not refresh and self._catalog is not None and now < self._catalog_expires_at:
                return self._catalog
            catalog = await self._fetch_voice_catalog()
            self._catalog = catalog
            self._catalog_expires_at = now + self.catalog_ttl
            return catalog

    async def synthesize(self, request: TTSSynthesisRequest) -> bytes:
        if not request.text.strip():
            return b""
        catalog = await self.get_voice_catalog()
        selection = resolve_selection(
            request,
            catalog,
            default_version=self.default_version,
            default_model=self.default_model,
            default_reference_language=self.default_reference_language,
            default_emotion=self.default_emotion,
            default_text_language=self.default_text_language,
            default_speed=self.default_speed,
        )
        other_params = dict(self.inference_params)
        for reserved_key in ("app_key", "text_lang", "prompt_lang", "emotion"):
            other_params.pop(reserved_key, None)
        other_params.update(
            {
                "text_lang": selection.text_language,
                "prompt_lang": selection.reference_language,
                "emotion": selection.emotion,
                "text_split_method": other_params.get("text_split_method", DEFAULT_SPLIT_METHOD),
            }
        )
        payload: JsonObject = {
            "model": f"GSVI-{selection.version}",
            "input": request.text,
            "voice": selection.model_name,
            "response_format": self.audio_format,
            "speed": selection.speed,
            "other_params": other_params,
        }
        try:
            response = await self._client.post(
                "/v1/audio/speech",
                json=payload,
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:
            raise map_request_error(exc) from exc
        audio = ensure_audio_response(response)
        if not audio:
            raise TTSSynthesisError("GPT-SoVITS returned empty audio")
        return audio

    async def close(self) -> None:
        await self._client.aclose()
