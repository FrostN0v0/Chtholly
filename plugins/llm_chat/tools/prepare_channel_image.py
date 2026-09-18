"""Generation-authorized channel image preparation."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable

from arclet.entari import Image, Session
from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from ..core.types import JSONType
from ..perception import PerceptionProvider
from ._registration import register_tool
from ..core.delivery import DeliveryError
from ..channel_images import ChannelImageReferenceError, resolve_channel_image_source
from ..prepared_media import prepare_media, ensure_media_capacity
from ..core.image_source import fetch_image_bytes, raw_to_image_data_url

WarningSink = Callable[[str], object]
_ALLOWED_INLINE_MIMES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})


@dataclass
class ChannelImageToolContext:
    """Dependencies for preparing one generation-authorized channel image."""

    get_perception: PerceptionProvider
    warn: WarningSink


async def _resolve_source(
    session: Session,
    image_ref: str,
    runtime: ChannelImageToolContext,
) -> str:
    try:
        return await resolve_channel_image_source(session, image_ref, runtime.get_perception())
    except ChannelImageReferenceError as exc:
        raise DeliveryError(str(exc)) from exc


async def _build_channel_image(
    session: Session,
    image_ref: str,
    runtime: ChannelImageToolContext,
) -> tuple[Image, int]:
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
    return Image.of(url=data_url), len(data)


def register_prepare_channel_image(
    dispatcher: PluginDispatcher[JSONType],
    runtime: ChannelImageToolContext,
) -> Subscriber[JSONType]:
    """Register preparation for generation-authorized channel images."""

    async def prepare_channel_image(session: Session, image_ref: str) -> dict[str, JSONType]:
        """Prepare one image exposed by an on-demand channel lookup without sending it.

        Use only an exact opaque image_ref returned by read_channel_messages or
        describe_channel_participant_avatar. Visual recognition is not required
        before sending. The reference is generation-local, cannot be guessed or
        reused later, and must never be shown to the user. The original image is
        downloaded into bounded inline bytes. Pass the returned media_ref to send_msg.

        Args:
            image_ref: Exact opaque image_ref from current channel context.

        Returns:
            dict: Prepared media reference without its original source.
        """
        ensure_media_capacity(1, byte_count=0)

        image, byte_count = await _build_channel_image(session, image_ref, runtime)
        return prepare_media(
            session,
            image,
            byte_count=byte_count,
            tool_name="prepare_channel_image",
            history_marker="[发送了图片]",
        )

    return register_tool(dispatcher, prepare_channel_image)
