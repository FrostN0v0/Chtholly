"""send_external_image LLM tool implementation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from collections.abc import Callable, Awaitable

from arclet.entari import Image, Session, MessageChain
from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from ._delivery import send_with_delivery
from ..core.types import JSONType
from ..web.policy import WebAccessError, normalize_public_url
from ._registration import register_tool
from ..core.delivery import DeliveryError, reserve_media_message, current_llm_chat_delivery
from ..core.image_source import IMAGE_FETCH_MAX_BYTES, fetch_image_bytes, raw_to_image_data_url

HistoryAppender = Callable[[str, str, str, str, str], Awaitable[object]]
WarningSink = Callable[[str], object]

_ALLOWED_INLINE_MIMES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})
_MAX_INLINE_SOURCE_CHARS = ((IMAGE_FETCH_MAX_BYTES + 2) // 3) * 4 + 256


@dataclass
class ExternalImageToolContext:
    """Mutable dependencies for sending non-catalog image sources."""

    append_history: HistoryAppender
    warn: WarningSink


def _normalize_inline_source(source: str) -> str:
    if len(source) > _MAX_INLINE_SOURCE_CHARS:
        raise DeliveryError("Inline image data is invalid or too large")
    lowered = source.lower()
    if lowered.startswith("data:"):
        header, separator, payload = source.partition(",")
        if not separator:
            raise DeliveryError("Inline image data is invalid or too large")
        return f"{header},{''.join(payload.split())}"
    if lowered.startswith("base64://"):
        return f"base64://{''.join(source[9:].split())}"
    if "://" in source:
        raise DeliveryError("Image source must be a public HTTP(S) URL or supported base64 image data")
    return f"base64://{''.join(source.split())}"


async def _build_image(session: Session, source: str) -> Image:
    candidate = source.strip()
    if not candidate:
        raise DeliveryError("Image source is required")

    lowered = candidate.lower()
    if lowered.startswith(("http://", "https://")):
        try:
            normalized_url = normalize_public_url(candidate)
        except WebAccessError as exc:
            raise DeliveryError("A valid public image URL is required") from exc
        return Image.of(url=normalized_url)

    inline_source = _normalize_inline_source(candidate)
    data = await fetch_image_bytes(session, inline_source)
    if data is None:
        raise DeliveryError("Inline image data is invalid or too large")
    data_url = raw_to_image_data_url(data)
    if data_url is None:
        raise DeliveryError("Inline image format is not recognized")
    mime = data_url[5:].partition(";")[0].lower()
    if mime not in _ALLOWED_INLINE_MIMES:
        raise DeliveryError("Inline image format must be JPEG, PNG, WebP, or GIF")
    return Image.of(raw=data)


def register_send_external_image(
    dispatcher: PluginDispatcher[JSONType],
    runtime: ExternalImageToolContext,
) -> Subscriber[JSONType]:
    """Register public-URL and inline-base64 image delivery."""

    async def send_external_image(session: Session, source: str) -> str:
        """Send one image from a public URL or bounded inline base64 data.

        Use this when another tool or the user provides a direct public image URL, a data:image/...;base64 value,
        a base64:// value, or raw base64 image data. This is the delivery step after finding an external image; it
        does not search, generate, inspect, or permanently save the image. Do not pass webpage URLs, private network
        URLs, credential-bearing URLs, local file paths, attachment handles, or arbitrary non-image data. Use
        send_image instead for registered local reaction images and stickers.

        Args:
            source (str): One direct public HTTP(S) image URL or JPEG, PNG, WebP, or GIF base64 source.
        Returns:
            str: Privacy-safe delivery status without echoing the source.
        """

        image = await _build_image(session, source)
        delivery_state = current_llm_chat_delivery()
        if delivery_state is not None:
            delivery_state = reserve_media_message()
        await send_with_delivery(session, MessageChain([image]), delivery_state, media=True)
        try:
            await runtime.append_history(session.channel.id, "", "bot", "assistant", "[发送了图片]")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            runtime.warn(f"external image delivery history failed: {type(exc).__name__}")
        return "已发送 1 张外部图片；不要在最终回复中重复，若无需补充只返回 [END_OF_RESPONSE]。"

    return register_tool(dispatcher, send_external_image)
