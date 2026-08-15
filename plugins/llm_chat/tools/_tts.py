"""Shared TTS tool protocols and catalog serialization."""

from __future__ import annotations

import json
from typing import Protocol
from collections.abc import Callable

from utils.tts_service_core.voice_catalog import TTSVoiceCatalog, TTSSynthesisSelection


class TTSServiceLike(Protocol):
    file_extension: str

    async def get_voice_catalog(self, *, refresh: bool = False) -> TTSVoiceCatalog: ...

    async def synthesize(
        self,
        text: str,
        *,
        version: str = "",
        model_name: str = "",
        reference_language: str = "",
        emotion: str = "",
        text_language: str = "",
        speed: float | None = None,
    ) -> bytes: ...


ServiceFactory = Callable[[], TTSServiceLike]


def _selection_payload(selection: TTSSynthesisSelection | None) -> dict[str, object] | None:
    if selection is None:
        return None
    return {
        "version": selection.version,
        "model_name": selection.model_name,
        "reference_language": selection.reference_language,
        "emotion": selection.emotion,
        "text_language": selection.text_language,
        "speed": selection.speed,
    }


def serialize_voice_catalog(catalog: TTSVoiceCatalog) -> str:
    return json.dumps(
        {
            "provider": catalog.provider,
            "voice_selection_available": bool(catalog.voices),
            "supports_inline_style_tags": catalog.supports_inline_style_tags,
            "text_languages": list(catalog.text_languages),
            "audio_formats": list(catalog.audio_formats),
            "speed": {
                "minimum": catalog.speed_min,
                "maximum": catalog.speed_max,
                "default": catalog.speed_default,
            },
            "default_selection": _selection_payload(catalog.default_selection),
            "voices": [
                {
                    "version": voice.version,
                    "model_name": voice.model_name,
                    "references": [
                        {
                            "language": reference.language,
                            "emotions": list(reference.emotions),
                        }
                        for reference in voice.references
                    ],
                }
                for voice in catalog.voices
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
