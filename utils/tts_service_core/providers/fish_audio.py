"""Fish Audio REST JSON TTS provider."""

import httpx

from .base import TTSSynthesisError
from .types import JsonObject
from .http_utils import map_request_error, ensure_audio_response
from ..voice_catalog import TTSVoiceCatalog, TTSSynthesisRequest

_ALLOWED_AUDIO_FORMATS = {"wav", "pcm", "mp3", "opus"}
_AUDIO_FORMATS = ("wav", "pcm", "mp3", "opus")
_SPEED_MIN = 0.5
_SPEED_MAX = 2.0


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
        extra_params: JsonObject | None = None,
        client: httpx.AsyncClient | None = None,
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
        self._client = client or httpx.AsyncClient(timeout=timeout)

    @property
    def file_extension(self) -> str:
        if self.audio_format in _ALLOWED_AUDIO_FORMATS:
            return f".{self.audio_format}"
        return ".mp3"

    async def get_voice_catalog(self, *, refresh: bool = False) -> TTSVoiceCatalog:
        del refresh
        return TTSVoiceCatalog(
            provider="fish-audio",
            voices=(),
            text_languages=(),
            audio_formats=_AUDIO_FORMATS,
            default_selection=None,
            supports_inline_style_tags=True,
            speed_min=_SPEED_MIN,
            speed_max=_SPEED_MAX,
            speed_default=self.prosody_speed,
        )

    async def synthesize(self, request: TTSSynthesisRequest) -> bytes:
        if not request.text.strip():
            return b""
        if any(
            (
                request.version,
                request.model_name,
                request.reference_language,
                request.emotion,
                request.text_language,
            )
        ):
            raise TTSSynthesisError("Fish Audio does not support GPT-SoVITS voice selectors")
        api_key = self.api_key.strip() if self.api_key is not None else ""
        if not api_key:
            raise TTSSynthesisError("Fish Audio API key is required")

        speed_value = self.prosody_speed if request.speed is None else request.speed
        if isinstance(speed_value, bool) or not isinstance(speed_value, (int, float)):
            raise TTSSynthesisError("Fish Audio speed must be numeric")
        speed = float(speed_value)
        if not _SPEED_MIN <= speed <= _SPEED_MAX:
            raise TTSSynthesisError(f"Fish Audio speed must be between {_SPEED_MIN} and {_SPEED_MAX}")

        prosody: JsonObject = {
            "speed": speed,
            "volume": self.prosody_volume,
            "normalize_loudness": self.prosody_normalize_loudness,
        }
        payload: JsonObject = {
            "text": request.text,
            "format": self.audio_format,
            "latency": self.latency,
            "mp3_bitrate": self.mp3_bitrate,
            "prosody": prosody,
        }
        if self.reference_id:
            payload["reference_id"] = self.reference_id
        if self.sample_rate is not None:
            payload["sample_rate"] = self.sample_rate
        payload.update(self.extra_params)

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
        except httpx.HTTPError as exc:
            raise map_request_error(exc) from exc
        return ensure_audio_response(resp)

    async def close(self) -> None:
        await self._client.aclose()
