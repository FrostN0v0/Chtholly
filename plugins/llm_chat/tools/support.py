"""Shared import-safe helpers for llm_chat tool implementations."""

from collections.abc import Sequence


def is_command_allowed(command_line: str, allowed_commands: Sequence[str]) -> tuple[bool, str]:
    """Return whether a command head is explicitly whitelisted."""
    head = command_line.split(maxsplit=1)[0] if command_line.strip() else ""
    return bool(head and head in allowed_commands), head


def _sentence_truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for punct in ("。", "！", "？", "!", "?", "."):
        idx = cut.rfind(punct)
        if idx > 0:
            return cut[: idx + 1]
    return cut


def truncate_for_tts(text: str, limit: int) -> str:
    """Truncate synthesized speech input at a natural boundary."""
    return _sentence_truncate(text, limit)


def audio_mime_type(suffix: str) -> str:
    """Return the MIME type used for an inline synthesized audio payload."""
    normalized = suffix.lower().lstrip(".")
    return {
        "wav": "audio/wav",
        "mp3": "audio/mpeg",
        "pcm": "audio/L16",
        "opus": "audio/ogg",
        "ogg": "audio/ogg",
        "aac": "audio/aac",
    }.get(normalized, "application/octet-stream")


__all__ = ["audio_mime_type", "truncate_for_tts", "is_command_allowed"]
