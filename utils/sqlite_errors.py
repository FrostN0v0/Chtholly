"""Name SQLite failures for logs without exposing SQL text or bound parameters."""

from __future__ import annotations


def sqlite_error_label(error: BaseException) -> str:
    """Return the exception type plus the driver's SQLite result code name, if any."""
    driver_error = getattr(error, "orig", None)
    code_name = getattr(driver_error, "sqlite_errorname", None)
    if isinstance(code_name, str) and code_name:
        return f"{type(error).__name__}({code_name})"
    return type(error).__name__
