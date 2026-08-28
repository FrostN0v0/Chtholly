"""Configuration model for the TTS service plugin."""

from typing import Any, Literal
from dataclasses import field

from arclet.entari import BasicConfModel


class TTSConfig(BasicConfModel):
    provider: Literal["gpt-sovits", "fish-audio"] = "gpt-sovits"
    """TTS backend provider."""
    timeout: float = 300.0
    """Request timeout in seconds."""
    gpt_sovits_base_url: str = "http://127.0.0.1:9874"
    """Base URL of the authenticated GSVI-compatible GPT-SoVITS adapter."""
    gpt_sovits_api_key: str | None = None
    """Bearer token for GPT-SoVITS catalog and synthesis requests."""
    gpt_sovits_default_version: str = ""
    """Preferred GPT-SoVITS version; empty selects the first available version."""
    gpt_sovits_default_model: str = ""
    """Preferred character model; empty selects the first model in the chosen version."""
    gpt_sovits_default_reference_language: str = ""
    """Preferred reference language; empty selects the first option for the model."""
    gpt_sovits_default_emotion: str = ""
    """Preferred reference emotion; empty prefers the provider's default emotion."""
    gpt_sovits_text_language: str = "\u4e2d\u6587"
    """Default synthesis language label accepted by the GSVI adapter."""
    gpt_sovits_speed: float = 1.0
    """Default GPT-SoVITS speech speed."""
    gpt_sovits_format: Literal["wav", "ogg", "mp3", "aac"] = "wav"
    """GPT-SoVITS output audio format."""
    gpt_sovits_catalog_ttl: float = 300.0
    """Seconds to cache discovered versions, models, references, and emotions."""
    gpt_sovits_extra_params: dict[str, Any] = field(default_factory=dict)
    """Low-level GSVI other_params merged before validated semantic selectors."""
    fish_api_url: str = "https://api.fish.audio/v1/tts"
    """Fish Audio TTS endpoint."""
    fish_api_key: str | None = None
    """Fish Audio API key; set from env in entari.yml."""
    fish_model: str = "s2-pro"
    """Fish Audio model header value."""
    fish_reference_id: str | None = None
    """Fish Audio voice model id. None uses Fish Audio's default voice."""
    fish_format: Literal["wav", "pcm", "mp3", "opus"] = "mp3"
    """Fish Audio output format."""
    fish_sample_rate: int | None = None
    """Optional Fish Audio sample_rate; None uses the format default."""
    fish_mp3_bitrate: Literal[64, 128, 192] = 128
    """Fish Audio MP3 bitrate in kbps."""
    fish_latency: Literal["low", "normal", "balanced"] = "normal"
    """Fish Audio latency-quality mode."""
    fish_prosody_speed: float = 1.0
    """Fish Audio prosody.speed."""
    fish_prosody_volume: float = 0.0
    """Fish Audio prosody.volume in dB."""
    fish_prosody_normalize_loudness: bool = True
    """Fish Audio prosody.normalize_loudness."""
    fish_extra_params: dict[str, Any] = field(default_factory=dict)
    """Extra Fish Audio request payload keys merged after built-in keys."""
