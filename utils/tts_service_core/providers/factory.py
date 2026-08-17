"""Provider factory for the TTS service."""

from __future__ import annotations

from typing import Literal, Protocol

from .base import TTSProvider, TTSSynthesisError
from .types import JsonObject
from .fish_audio import FishAudioProvider
from .gpt_sovits import GptSovitsProvider


class TTSConfigLike(Protocol):
    provider: Literal["gpt-sovits", "fish-audio"]
    timeout: float
    gpt_sovits_base_url: str
    gpt_sovits_api_key: str | None
    gpt_sovits_default_version: str
    gpt_sovits_default_model: str
    gpt_sovits_default_reference_language: str
    gpt_sovits_default_emotion: str
    gpt_sovits_text_language: str
    gpt_sovits_speed: float
    gpt_sovits_format: Literal["wav", "ogg", "mp3", "aac"]
    gpt_sovits_catalog_ttl: float
    gpt_sovits_extra_params: JsonObject
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
            config.gpt_sovits_base_url,
            config.gpt_sovits_api_key,
            timeout=config.timeout,
            default_version=config.gpt_sovits_default_version,
            default_model=config.gpt_sovits_default_model,
            default_reference_language=config.gpt_sovits_default_reference_language,
            default_emotion=config.gpt_sovits_default_emotion,
            default_text_language=config.gpt_sovits_text_language,
            default_speed=config.gpt_sovits_speed,
            audio_format=config.gpt_sovits_format,
            catalog_ttl=config.gpt_sovits_catalog_ttl,
            inference_params=config.gpt_sovits_extra_params,
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
