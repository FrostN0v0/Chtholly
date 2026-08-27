"""Shared values for bounded public webpage screenshots."""

from __future__ import annotations

from dataclasses import dataclass


class WebScreenshotError(RuntimeError):
    """Sanitized public-page screenshot failure."""


@dataclass(frozen=True, slots=True)
class ScreenshotRegion:
    x: float
    y: float
    width: float
    height: float
    matched: str | None
    truncated: bool


@dataclass(frozen=True, slots=True)
class WebScreenshot:
    data: bytes
    matched_section: str | None
    truncated: bool
