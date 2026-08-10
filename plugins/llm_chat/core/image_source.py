"""Bounded image-source decoding shared by vision and delivery tools."""

from __future__ import annotations

import base64
import asyncio
from pathlib import Path
import binascii

from arclet.entari import Image, Session

_IMAGE_FETCH_TIMEOUT = 15.0
IMAGE_FETCH_MAX_BYTES = 6 * 1024 * 1024
_IMAGE_BASE64_MAX_CHARS = ((IMAGE_FETCH_MAX_BYTES + 2) // 3) * 4


def raw_to_image_data_url(data: bytes) -> str | None:
    """Convert image bytes to a data URL using Satori MIME sniffing."""

    try:
        src = Image.of(raw=data).src
    except ValueError:
        return None
    return src if src.startswith("data:image/") else None


def image_file_to_data_url(path: Path, *, max_bytes: int = IMAGE_FETCH_MAX_BYTES) -> str | None:
    """Read an image file and convert it to a sniffed data URL."""

    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) > max_bytes:
        return None
    return raw_to_image_data_url(data)


def _decode_inline_base64(payload: str) -> bytes | None:
    if len(payload) > _IMAGE_BASE64_MAX_CHARS:
        return None
    try:
        data = base64.b64decode(payload, validate=True)
    except (ValueError, binascii.Error):
        return None
    return data if len(data) <= IMAGE_FETCH_MAX_BYTES else None


async def fetch_image_bytes(session: Session, src: str) -> bytes | None:
    """Resolve one supported image source to bounded raw bytes."""

    if src.startswith("data:"):
        header, separator, payload = src.partition(",")
        if not separator or not header.lower().startswith("data:image/") or not header.lower().endswith(";base64"):
            return None
        return _decode_inline_base64(payload)
    if src.startswith("base64://"):
        return _decode_inline_base64(src[9:])
    try:
        data = await asyncio.wait_for(session.download(src), timeout=_IMAGE_FETCH_TIMEOUT)
    except Exception:
        return None
    if not isinstance(data, bytes) or len(data) > IMAGE_FETCH_MAX_BYTES:
        return None
    return data


async def fetch_image_data_url(session: Session, src: str) -> str | None:
    """Resolve an image source to a validated image data URL."""

    data = await fetch_image_bytes(session, src)
    return raw_to_image_data_url(data) if data is not None else None
