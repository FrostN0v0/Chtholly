"""Configuration model for the TTS service plugin."""

from typing import Any, Literal
from dataclasses import field

from arclet.entari import BasicConfModel


class TTSConfig(BasicConfModel):
    provider: Literal["gpt-sovits", "fish-audio"] = "gpt-sovits"
    """TTS backend provider."""
    api_url: str = "http://127.0.0.1:9880/tts"
    """Synthesis endpoint of the provider."""
    default_speaker: str = ""
    """Default speaker/voice name passed to the provider."""
    timeout: float = 15.0
    """Request timeout in seconds."""
    text_lang: str = "zh"
    """Language of the input text (gpt-sovits `text_lang`)."""
    extra_params: dict[str, Any] = field(default_factory=dict)
    """Extra provider-specific parameters merged into every request payload
    (e.g. ref_audio_path / prompt_text / prompt_lang for gpt-sovits v2)."""
    fish_api_url: str = "https://api.fish.audio/v1/tts"
    """Fish Audio TTS endpoint."""
    fish_api_key: str | None = None
    """Fish Audio API key; set from env in entari.yml."""
    fish_model: str = "s2-pro"
    """Fish Audio model header value. OpenAPI lists s1/s2-pro; product docs also mention newer s2.1 variants."""
    fish_reference_id: str | None = None
    """Fish Audio voice model id. None uses Fish Audio's default voice."""
    fish_format: Literal["wav", "pcm", "mp3", "opus"] = "mp3"
    """Fish Audio output format."""
    fish_sample_rate: int | None = None
    """Optional Fish Audio sample_rate; None uses the format default."""
    fish_mp3_bitrate: Literal[64, 128, 192] = 128
    """Fish Audio MP3 bitrate in kbps."""
    fish_latency: Literal["low", "normal", "balanced"] = "normal"
    """Fish Audio latency-quality mode. OpenAPI default is normal."""
    fish_prosody_speed: float = 1.0
    """Fish Audio prosody.speed."""
    fish_prosody_volume: float = 0.0
    """Fish Audio prosody.volume in dB."""
    fish_prosody_normalize_loudness: bool = True
    """Fish Audio prosody.normalize_loudness."""
    fish_extra_params: dict[str, Any] = field(default_factory=dict)
    """Extra Fish Audio request payload keys merged after built-in keys."""
