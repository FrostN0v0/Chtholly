"""Provider factory for the TTS service."""

from __future__ import annotations

from typing import Literal, Protocol

from .base import TTSProvider, TTSSynthesisError
from .types import JsonObject
from .fish_audio import FishAudioProvider
from .gpt_sovits import GptSovitsProvider


class TTSConfigLike(Protocol):
    provider: Literal["gpt-sovits", "fish-audio"]
    api_url: str
    default_speaker: str
    timeout: float
    text_lang: str
    extra_params: JsonObject
    fish_api_url: str
    fish_api_key: str | None
    fish_model: str
    fish_reference_id: str | None
    fish_format: Literal["wav", "pcm", "mp3", "opus"]
    fish_sample_rate: int | None
    fish_mp3_bitrate: Literal[64, 128, 192]
    fish_latency: Literal["low", "normal", "balanced"]
    fish_prosody_speed: float
    fish_prosody_volume: float
    fish_prosody_normalize_loudness: bool
    fish_extra_params: JsonObject


def build_provider(config: TTSConfigLike) -> TTSProvider:
    if config.provider == "gpt-sovits":
        return GptSovitsProvider(
            config.api_url,
            timeout=config.timeout,
            text_lang=config.text_lang,
            default_speaker=config.default_speaker,
            extra_params=config.extra_params,
        )
    if config.provider == "fish-audio":
        return FishAudioProvider(
            config.fish_api_url,
            config.fish_api_key,
            model=config.fish_model,
            reference_id=config.fish_reference_id,
            timeout=config.timeout,
            audio_format=config.fish_format,
            sample_rate=config.fish_sample_rate,
            mp3_bitrate=config.fish_mp3_bitrate,
            latency=config.fish_latency,
            prosody_speed=config.fish_prosody_speed,
            prosody_volume=config.fish_prosody_volume,
            prosody_normalize_loudness=config.fish_prosody_normalize_loudness,
            extra_params=config.fish_extra_params,
        )
    raise TTSSynthesisError(f"Unsupported TTS provider: {config.provider}")
