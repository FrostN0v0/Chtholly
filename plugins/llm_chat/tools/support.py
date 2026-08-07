"""Shared import-safe helpers for llm_chat tool implementations."""

from uuid import uuid4
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


def tts_temp_path(suffix: str = ".wav") -> str:
    """Return a unique temp path for a synthesized clip under local_data."""
    from arclet.entari import local_data

    if not suffix.startswith("."):
        suffix = f".{suffix}"
    if suffix not in {".wav", ".mp3", ".pcm", ".opus", ".ogg", ".aac"}:
        suffix = ".wav"
    return str(local_data.get_temp_file(f"tts_{uuid4().hex}{suffix}"))


__all__ = ["truncate_for_tts", "tts_temp_path", "is_command_allowed"]
