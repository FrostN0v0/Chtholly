"""Provider factory for the TTS service."""

from typing import Any

from .base import TTSProvider, TTSSynthesisError
from .fish_audio import FishAudioProvider
from .gpt_sovits import GptSovitsProvider


def build_provider(config: Any) -> TTSProvider:
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
