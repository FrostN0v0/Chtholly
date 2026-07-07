"""Configuration model for the TTS service plugin."""

from typing import Any, Literal
from dataclasses import field

from arclet.entari import BasicConfModel


class TTSConfig(BasicConfModel):
    provider: Literal["gpt-sovits"] = "gpt-sovits"
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
