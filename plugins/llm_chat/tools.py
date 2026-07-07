"""LLM tool helper functions for llm_chat.

Tool *handlers* are registered in ``__init__.py`` (so they are defined in
the same module as the plugin); this module only exposes pure helpers and
the truncation utility they share. The dispatch target ``_tools`` is built
in ``__init__`` and passed into ``register_tools`` there.
"""

from uuid import uuid4


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
    """Public alias used by the speak tool handler in __init__."""
    return _sentence_truncate(text, limit)


def tts_temp_path() -> str:
    """Return a unique temp path for a synthesized clip under local_data."""
    from arclet.entari import local_data

    return str(local_data.get_temp_file(f"tts_{uuid4().hex}.wav"))


__all__ = ["truncate_for_tts", "tts_temp_path"]
