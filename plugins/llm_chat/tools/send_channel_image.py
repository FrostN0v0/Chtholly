"""send_channel_image LLM tool implementation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from collections.abc import Callable, Awaitable

from arclet.entari import Image, Session, MessageChain
from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from ._delivery import send_with_delivery
from ..core.types import JSONType
from ..perception import PerceptionProvider
from ._registration import register_tool
from ..core.delivery import DeliveryError, reserve_media_message, current_llm_chat_delivery
from ..channel_images import (
    MessageImageTarget,
    ParticipantAvatarTarget,
    current_channel_image_references,
)
from ..core.image_source import fetch_image_bytes, raw_to_image_data_url

HistoryAppender = Callable[[str, str, str, str, str], Awaitable[object]]
WarningSink = Callable[[str], object]
_ALLOWED_INLINE_MIMES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})


@dataclass
class ChannelImageToolContext:
    """Mutable dependencies for sending one generation-authorized channel image."""

    get_perception: PerceptionProvider
    append_history: HistoryAppender
    warn: WarningSink


async def _resolve_source(
    session: Session,
    image_ref: str,
    runtime: ChannelImageToolContext,
) -> str:
    references = current_channel_image_references()
    target = references.resolve(image_ref.strip()) if references is not None else None
    if isinstance(target, ParticipantAvatarTarget):
        return target.source
    if not isinstance(target, MessageImageTarget):
        raise DeliveryError("A valid image_ref from current channel context is required")
    try:
        sources = await runtime.get_perception().message_image_sources(session, target.cursor)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise DeliveryError("The channel image is no longer available") from exc
    if target.image_index > len(sources):
        raise DeliveryError("The channel image is no longer available")
    return sources[target.image_index - 1]


async def _build_channel_image(
    session: Session,
    image_ref: str,
    runtime: ChannelImageToolContext,
) -> Image:
    source = await _resolve_source(session, image_ref, runtime)
    data = await fetch_image_bytes(session, source)
    if data is None:
        raise DeliveryError("The channel image could not be downloaded")
    data_url = raw_to_image_data_url(data)
    if data_url is None:
        raise DeliveryError("The channel image format is not recognized")
    mime = data_url[5:].partition(";")[0].lower()
    if mime not in _ALLOWED_INLINE_MIMES:
        raise DeliveryError("Channel images must be JPEG, PNG, WebP, or GIF")
    return Image.of(raw=data)


def register_send_channel_image(
    dispatcher: PluginDispatcher[JSONType],
    runtime: ChannelImageToolContext,
) -> Subscriber[JSONType]:
    """Register delivery for generation-authorized channel images."""

    async def send_channel_image(session: Session, image_ref: str) -> str:
        """Send one image already exposed in the current channel context.

        Use only an exact opaque image_ref returned in ambient_channel_context,
        by read_channel_messages, or by describe_channel_participant_avatar. The
        reference is generation-local, cannot be guessed or reused later, and
        must never be shown to the user. The original image is downloaded and
        sent as inline bytes without exposing its source URL.

        Args:
            image_ref: Exact opaque image_ref from current channel context.

        Returns:
            str: Privacy-safe delivery status without echoing the reference.
        """

        image = await _build_channel_image(session, image_ref, runtime)
        delivery_state = current_llm_chat_delivery()
        if delivery_state is not None:
            delivery_state = reserve_media_message()
        await send_with_delivery(session, MessageChain([image]), delivery_state, media=True)
        try:
            await runtime.append_history(session.channel.id, "", "bot", "assistant", "[发送了图片]")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            runtime.warn(f"channel image delivery history failed: {type(exc).__name__}")
        return "已发送 1 张群聊图片；不要在最终回复中重复，若无需补充只返回 [END_OF_RESPONSE]。"

    return register_tool(dispatcher, send_channel_image)
