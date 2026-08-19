"""Trusted self-reference image injection for native image generation."""

from __future__ import annotations

from typing import Any, cast
from pathlib import Path, PurePosixPath
from functools import lru_cache
from collections.abc import Callable

from utils.path import IMAGE_DIR

from .types import ChatMessage
from .image_source import image_file_to_data_url

SELF_REFERENCE_IMAGE_MARKER = "[当前角色自设参考图]"
_UNAVAILABLE_WARNING = "self reference image skipped: configured file unavailable"
_INVALID_WARNING = "self reference image skipped: configured file invalid or too large"


def resolve_self_reference_image(
    relative_path: str | None,
    *,
    image_root: Path = IMAGE_DIR,
) -> Path | None:
    """Resolve one configured path strictly below the static image root."""

    if not isinstance(relative_path, str):
        return None
    normalized = relative_path.strip().replace("\\", "/")
    if not normalized:
        return None
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or not pure.parts or ":" in pure.parts[0]:
        return None
    if any(part in {"", ".", ".."} for part in pure.parts):
        return None

    try:
        root = image_root.resolve()
        candidate = root.joinpath(*pure.parts)
        if candidate.is_symlink():
            return None
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    return resolved if resolved.is_file() else None


@lru_cache(maxsize=4)
def _cached_image_data_url(path: Path, _mtime_ns: int, _size: int) -> str | None:
    return image_file_to_data_url(path)


def _image_data_url(path: Path) -> str | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return _cached_image_data_url(path, stat.st_mtime_ns, stat.st_size)


def append_self_reference_image(
    messages: list[ChatMessage],
    relative_path: str | None,
    warn: Callable[[str], None],
    *,
    image_root: Path = IMAGE_DIR,
) -> bool:
    """Append the trusted role reference to the latest user turn without persisting it."""

    if not relative_path:
        return False
    path = resolve_self_reference_image(relative_path, image_root=image_root)
    if path is None:
        warn(_UNAVAILABLE_WARNING)
        return False
    data_url = _image_data_url(path)
    if data_url is None:
        warn(_INVALID_WARNING)
        return False

    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            parts: list[dict[str, Any]] = [{"type": "text", "text": content}]
        elif isinstance(content, list):
            parts = list(content)
        else:
            warn(_UNAVAILABLE_WARNING)
            return False
        parts.extend(
            (
                {"type": "text", "text": SELF_REFERENCE_IMAGE_MARKER},
                {"type": "image_url", "image_url": {"url": data_url}},
            )
        )
        updated = dict(message)
        updated["content"] = parts
        messages[index] = cast(ChatMessage, updated)
        return True

    warn(_UNAVAILABLE_WARNING)
    return False
