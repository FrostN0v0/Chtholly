"""Safe exception summaries for runtime diagnostics."""

from __future__ import annotations

import re

_MAX_CHAIN_DEPTH = 6
_MAX_MESSAGE_LENGTH = 400
_SECRET_PATTERNS = (
    (re.compile(r"(?i)\bBearer\s+[^\s,;]+"), "Bearer [REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"), "sk-[REDACTED]"),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|access[_-]?token|authorization|token)"
            r"(\s*[:=]\s*)([^\s,;\]}]+)"
        ),
        r"\1\2[REDACTED]",
    ),
    (re.compile(r"(?i)(https?://[^\s?]+)\?[^\s]+"), r"\1?[REDACTED]"),
)


def _sanitize_message(message: str) -> str:
    sanitized = " ".join(message.split())
    for pattern, replacement in _SECRET_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)
    if len(sanitized) <= _MAX_MESSAGE_LENGTH:
        return sanitized
    return f"{sanitized[: _MAX_MESSAGE_LENGTH - 3]}..."


def summarize_exception(exc: BaseException) -> str:
    """Render a bounded, redacted exception chain for operational logs."""

    parts: list[str] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen and len(parts) < _MAX_CHAIN_DEPTH:
        seen.add(id(current))
        message = _sanitize_message(str(current))
        parts.append(f"{type(current).__name__}: {message}" if message else type(current).__name__)
        if current.__cause__ is not None:
            current = current.__cause__
        elif not current.__suppress_context__:
            current = current.__context__
        else:
            current = None
    return " <- ".join(parts)
