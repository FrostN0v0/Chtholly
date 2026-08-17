"""Typed voice catalog and synthesis request models."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TTSReferenceOption:
    language: str
    emotions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TTSVoiceOption:
    version: str
    model_name: str
    references: tuple[TTSReferenceOption, ...]


@dataclass(frozen=True, slots=True)
class TTSSynthesisSelection:
    version: str
    model_name: str
    reference_language: str
    emotion: str
    text_language: str
    speed: float


@dataclass(frozen=True, slots=True)
class TTSVoiceCatalog:
    provider: str
    voices: tuple[TTSVoiceOption, ...]
    text_languages: tuple[str, ...]
    audio_formats: tuple[str, ...]
    default_selection: TTSSynthesisSelection | None
    supports_inline_style_tags: bool
    speed_min: float
    speed_max: float
    speed_default: float


@dataclass(frozen=True, slots=True)
class TTSSynthesisRequest:
    text: str
    version: str | None = None
    model_name: str | None = None
    reference_language: str | None = None
    emotion: str | None = None
    text_language: str | None = None
    speed: float | None = None
