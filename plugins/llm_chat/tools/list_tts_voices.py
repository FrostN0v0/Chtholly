"""list_tts_voices LLM tool implementation."""

from __future__ import annotations

import json
from dataclasses import dataclass

from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from ._tts import ServiceFactory, serialize_voice_catalog
from ..core.types import JSONType
from ._registration import register_tool


@dataclass
class TTSVoiceToolContext:
    """Mutable dependencies for TTS voice catalog access."""

    enabled: bool
    get_service: ServiceFactory


def register_list_tts_voices(
    dispatcher: PluginDispatcher[JSONType],
    runtime: TTSVoiceToolContext,
) -> Subscriber[JSONType] | None:
    """Register the read-only TTS voice catalog tool."""

    if not runtime.enabled:
        return None

    async def list_tts_voices(refresh: bool = False) -> str:
        """List current TTS provider capabilities and selectable GPT-SoVITS voices.

        Call this before speak whenever the user names a character/model, version, reference language, or emotion, or
        when choosing a GPT-SoVITS voice for expressive delivery. The returned version, model_name, reference language,
        emotion, text language, and speed values are authoritative and may change at runtime. Pass exact values to
        speak. Never invent a missing option or silently substitute another character. Fish Audio reports no selectable
        catalog and instead indicates whether inline style tags are supported.

        Args:
            refresh (bool): Bypass the short provider catalog cache only when newly installed voices are expected.
        Returns:
            str: Compact JSON describing the active provider, defaults, ranges, and available voice hierarchy.
        """

        force_refresh = refresh if type(refresh) is bool else False
        try:
            catalog = await runtime.get_service().get_voice_catalog(refresh=force_refresh)
        except Exception:
            return json.dumps(
                {"available": False, "error": "TTS voice catalog is unavailable"},
                separators=(",", ":"),
            )
        return serialize_voice_catalog(catalog)

    return register_tool(dispatcher, list_tts_voices)
