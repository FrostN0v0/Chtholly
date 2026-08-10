"""Static resource directory constants for Chtholly plugins."""

from pathlib import Path

RES_DIR = Path(__file__).resolve().parent.parent / "resources"
"""Static resources root (shipped with the repo)."""

IMAGE_DIR = RES_DIR / "image"
"""Image resources directory."""

MEME_DIR = IMAGE_DIR / "memes"
"""Collected reaction images directory."""

AUDIO_DIR = RES_DIR / "audio"
"""Audio resources directory."""

FONT_DIR = RES_DIR / "font"
"""Font resources directory."""
