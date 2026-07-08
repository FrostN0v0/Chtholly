"""Fish Audio REST JSON TTS provider."""

from typing import Any

import httpx

from .base import TTSSynthesisError

_ALLOWED_AUDIO_FORMATS = {"wav", "pcm", "mp3", "opus"}


class FishAudioProvider:
    def __init__(
        self,
        api_url: str,
        api_key: str | None,
        *,
        model: str = "s2-pro",
        reference_id: str | None = None,
        timeout: float = 15.0,
        audio_format: str = "mp3",
        sample_rate: int | None = None,
        mp3_bitrate: int = 128,
        latency: str = "normal",
        prosody_speed: float = 1.0,
        prosody_volume: float = 0.0,
        prosody_normalize_loudness: bool = True,
        extra_params: dict[str, Any] | None = None,
    ) -> None:
        self.api_url = api_url
        self.api_key = api_key
        self.model = model
        self.reference_id = reference_id
        self.audio_format = audio_format
        self.sample_rate = sample_rate
        self.mp3_bitrate = mp3_bitrate
        self.latency = latency
        self.prosody_speed = prosody_speed
        self.prosody_volume = prosody_volume
        self.prosody_normalize_loudness = prosody_normalize_loudness
        self.extra_params = extra_params or {}
        self._client = httpx.AsyncClient(timeout=timeout)

    @property
    def file_extension(self) -> str:
        if self.audio_format in _ALLOWED_AUDIO_FORMATS:
            return f".{self.audio_format}"
        return ".mp3"

    async def synthesize(self, text: str, **params: Any) -> bytes:
        if not text.strip():
            return b""
        api_key = self.api_key.strip() if self.api_key is not None else ""
        if not api_key:
            raise TTSSynthesisError("Fish Audio API key is required")

        payload: dict[str, Any] = {
            "text": text,
            "format": self.audio_format,
            "latency": self.latency,
            "mp3_bitrate": self.mp3_bitrate,
            "prosody": {
                "speed": self.prosody_speed,
                "volume": self.prosody_volume,
                "normalize_loudness": self.prosody_normalize_loudness,
            },
        }
        if self.reference_id:
            payload["reference_id"] = self.reference_id
        if self.sample_rate is not None:
            payload["sample_rate"] = self.sample_rate
        payload.update(self.extra_params)
        payload.update(params)

        try:
            resp = await self._client.post(
                self.api_url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "model": self.model,
                },
            )
        except httpx.TimeoutException as e:
            raise TTSSynthesisError(f"TTS request timed out: {e}") from e
        except httpx.HTTPError as e:
            raise TTSSynthesisError(f"TTS request failed: {e}") from e

        if resp.status_code != 200:
            detail: str
            try:
                detail = str(resp.json())
            except ValueError:
                detail = resp.text[:200]
            raise TTSSynthesisError(f"TTS provider returned {resp.status_code}: {detail}")

        content_type = resp.headers.get("content-type", "")
        if "application/json" in content_type:
            raise TTSSynthesisError(f"TTS provider returned JSON instead of audio: {resp.text[:200]}")
        return resp.content

    async def close(self) -> None:
        await self._client.aclose()
