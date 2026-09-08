"""Bounded JSON sanitization for persisted tool activity."""

from __future__ import annotations

import json
import math
from typing import cast
from urllib.parse import urlsplit, urlunsplit
from collections.abc import Mapping, Sequence

from .types import JSONType

MAX_ARGUMENT_TEXT = 500
MAX_RESULT_TEXT = 1200
MAX_SOURCE_SNIPPET = 320
MAX_WEB_SOURCES = 5
_MAX_COLLECTION_ITEMS = 20
_MAX_NESTING_DEPTH = 4
_REDACTED = "[REDACTED]"
_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "base64",
    "cookie",
    "credential",
    "password",
    "secret",
)


def _is_sensitive_key(normalized_key: str) -> bool:
    if any(part in normalized_key for part in _SENSITIVE_KEY_PARTS):
        return True
    if "token" not in normalized_key:
        return False
    return not (normalized_key.endswith("_tokens") or normalized_key.endswith("_token_count"))


def compact_tool_activity(
    items: Sequence[Mapping[str, object]],
    *,
    max_chars: int,
) -> list[dict[str, JSONType]]:
    """Prefer the newest safe activity records within one prompt budget."""

    budget = max(0, int(max_chars))
    if budget == 0:
        return []
    selected: list[dict[str, JSONType]] = []
    used = 2
    for item in reversed(items):
        sanitized = sanitize_json(item, max_text=MAX_RESULT_TEXT)
        if not isinstance(sanitized, dict):
            continue
        candidate = cast(dict[str, JSONType], sanitized)
        size = len(json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))) + (1 if selected else 0)
        if used + size > budget:
            continue
        selected.insert(0, candidate)
        used += size
    return selected


def sanitize_json(value: object, *, max_text: int, depth: int = 0) -> JSONType:
    """Convert arbitrary boundary data to bounded redacted JSON values."""

    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return compact_text(value, max_text)
    if isinstance(value, bytes):
        return {"type": "bytes", "size": len(value)}
    if depth >= _MAX_NESTING_DEPTH:
        return {"type": type(value).__name__}
    if isinstance(value, Mapping):
        result: dict[str, JSONType] = {}
        for index, (raw_key, raw_value) in enumerate(value.items()):
            if index >= _MAX_COLLECTION_ITEMS:
                break
            key = compact_text(raw_key, 80)
            if not key:
                continue
            normalized_key = key.casefold().replace("-", "_")
            if _is_sensitive_key(normalized_key):
                result[key] = _REDACTED
            else:
                result[key] = sanitize_json(raw_value, max_text=max_text, depth=depth + 1)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [sanitize_json(item, max_text=max_text, depth=depth + 1) for item in value[:_MAX_COLLECTION_ITEMS]]
    return {"type": type(value).__name__}


def selected_arguments(arguments: Mapping[str, object], *names: str) -> dict[str, JSONType]:
    """Project selected model arguments through the common sanitizer."""

    selected: dict[str, JSONType] = {}
    for name in names:
        if name in arguments:
            selected[name] = sanitize_json(arguments[name], max_text=MAX_ARGUMENT_TEXT)
    return selected


def parse_json_object(value: object) -> Mapping[str, object] | None:
    """Return a mapping from a direct or encoded JSON object."""

    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except ValueError:
        return None
    return parsed if isinstance(parsed, Mapping) else None


def safe_url(value: object) -> str:
    """Retain a public-looking URL shape without query parameters or credentials."""

    candidate = compact_text(value, MAX_ARGUMENT_TEXT)
    if not candidate:
        return ""
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError:
        return "[INVALID_URL]"
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return "[INVALID_URL]"
    netloc = parsed.hostname.casefold()
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urlunsplit((parsed.scheme.casefold(), netloc, parsed.path, "", ""))


def external_source_type(value: object) -> str:
    """Classify an external image source without retaining its payload."""

    if not isinstance(value, str):
        return "invalid"
    candidate = value.strip().casefold()
    if candidate.startswith(("http://", "https://")):
        return "public_url"
    if candidate.startswith(("data:image/", "base64://")):
        return "inline_data"
    return "raw_inline_data" if candidate else "empty"


def compact_text(value: object, limit: int) -> str:
    """Collapse and cap text without stringifying unknown complex objects."""

    if value is None:
        return ""
    if not isinstance(value, (str, int, float, bool)):
        return ""
    normalized = " ".join(str(value).split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: limit - 1]}…"


def text_length(value: object) -> int:
    return len(value) if isinstance(value, str) else 0


def safe_int(value: object) -> int:
    return value if type(value) is int else 0
