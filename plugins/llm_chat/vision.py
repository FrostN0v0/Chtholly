"""Vision-model boundary for inbound images and local image tagging."""

from __future__ import annotations

import base64
from typing import Protocol, cast
import asyncio
from pathlib import Path
import binascii

import litellm
from arclet.entari import Image, Session
from entari_plugin_llm.config import get_model_config

from .config import LLMChatConfig
from .core.media import normalize_image_tags, normalize_image_description


class _MessageLike(Protocol):
    content: str | None


class _ChoiceLike(Protocol):
    message: _MessageLike


class _CompletionLike(Protocol):
    choices: list[_ChoiceLike]


_IMAGE_FETCH_TIMEOUT = 15.0
VISION_DESCRIBE_TIMEOUT = 60.0
VISION_TAG_TIMEOUT = 120.0
IMAGE_FETCH_MAX_BYTES = 6 * 1024 * 1024
_IMAGE_BASE64_MAX_CHARS = ((IMAGE_FETCH_MAX_BYTES + 2) // 3) * 4
_IMAGE_DESC_CACHE_MAX = 128
_image_desc_cache: dict[str, str] = {}


async def vision_completion(
    config: LLMChatConfig,
    data_url: str,
    system_prompt: str,
    user_text: str,
    *,
    timeout: float,
) -> str:
    """Call the configured vision model and return stripped text content."""
    model = get_model_config(config.image_tag_model)
    response = await litellm.acompletion(
        model=model.name,
        messages=[
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ],
        base_url=model.base_url,
        api_key=model.api_key,
        timeout=timeout,
        **model.extra,
    )
    completion = cast(_CompletionLike, response)
    content = completion.choices[0].message.content
    return (content or "").strip()


def raw_to_image_data_url(data: bytes) -> str | None:
    """Convert image bytes to a data URL using Satori mime sniffing."""
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
    try:
        return raw_to_image_data_url(data)
    except ValueError:
        return None


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
    """Resolve an element src to a validated image data URL."""
    data = await fetch_image_bytes(session, src)
    return raw_to_image_data_url(data) if data is not None else None


async def describe_image(config: LLMChatConfig, session: Session, src: str) -> str:
    """Describe one inbound image; empty string means bare placeholder."""
    cached = _image_desc_cache.get(src)
    if cached is not None:
        return cached
    data_url = await fetch_image_data_url(session, src)
    if data_url is None:
        return ""
    raw = await vision_completion(
        config,
        data_url,
        config.image_describe_prompt,
        "Describe this chat image for conversation context.",
        timeout=VISION_DESCRIBE_TIMEOUT,
    )
    description = normalize_image_description(raw)
    if description:
        if len(_image_desc_cache) >= _IMAGE_DESC_CACHE_MAX:
            _image_desc_cache.pop(next(iter(_image_desc_cache)))
        _image_desc_cache[src] = description
    return description


async def generate_image_tags(config: LLMChatConfig, data_url: str) -> str:
    """Generate normalized local-image tags."""
    raw = await vision_completion(
        config,
        data_url,
        config.image_tag_prompt,
        "Tag this image for chat reaction retrieval.",
        timeout=VISION_TAG_TIMEOUT,
    )
    return normalize_image_tags(raw)
