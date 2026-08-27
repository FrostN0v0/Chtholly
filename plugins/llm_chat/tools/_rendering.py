"""Shared runtime and delivery primitives for rendered-image tools."""

from __future__ import annotations

from typing import TYPE_CHECKING
import asyncio
from pathlib import Path
from dataclasses import dataclass
from collections.abc import Callable, Awaitable

from arclet.entari import Image, Session, MessageChain

from ._delivery import send_with_delivery
from ..core.delivery import DeliveryError, reserve_media_message, current_llm_chat_delivery
from ..core.image_source import IMAGE_FETCH_MAX_BYTES

if TYPE_CHECKING:
    from entari_plugin_htmlrender import HtmlRenderer, RasterOptions, RenderedImage

HistoryAppender = Callable[[str, str, str, str, str], Awaitable[object]]
RendererGetter = Callable[[], "HtmlRenderer"]
WarningSink = Callable[[str], object]
RenderCall = Callable[["HtmlRenderer", "RasterOptions", float], Awaitable["RenderedImage"]]

DEFAULT_RENDER_WIDTH = 900
MIN_RENDER_WIDTH = 480
MAX_RENDER_WIDTH = 1200
MAX_RENDER_SOURCE_CHARS = 50_000
RENDER_TIMEOUT_SECONDS = 30.0
DEFAULT_RENDER_FONT_FAMILY = "Inter, Noto Sans SC, Noto Sans CJK SC, sans-serif"

_IMAGE_HISTORY_MARKER = "[发送了图片]"


@dataclass
class RenderToolContext:
    """Runtime dependencies shared by image-rendering tools."""

    get_renderer: RendererGetter
    append_history: HistoryAppender
    warn: WarningSink
    template_root: Path
    timeout_seconds: float = RENDER_TIMEOUT_SECONDS
    max_source_chars: int = MAX_RENDER_SOURCE_CHARS


def normalize_render_width(width: int) -> int:
    """Validate the logical viewport width exposed to the model."""

    if type(width) is not int or not MIN_RENDER_WIDTH <= width <= MAX_RENDER_WIDTH:
        raise DeliveryError(f"width must be an integer between {MIN_RENDER_WIDTH} and {MAX_RENDER_WIDTH}")
    return width


def render_options(width: int) -> RasterOptions:
    """Build one bounded, high-density PNG raster configuration."""

    from entari_plugin_htmlrender import RasterOptions

    return RasterOptions(width=normalize_render_width(width), device_pixel_ratio=1.5, format="png")


async def deliver_image_bytes(
    session: Session,
    data: bytes | bytearray | memoryview,
    *,
    append_history: HistoryAppender,
    warn: WarningSink,
    tool_name: str,
    success_message: str | None = None,
) -> str:
    """Validate, send, and persist one generated image payload."""

    raw = bytes(data)
    if not raw or len(raw) > IMAGE_FETCH_MAX_BYTES:
        raise DeliveryError("rendered image is empty or exceeds the delivery size limit")
    try:
        image = Image.of(raw=raw)
    except ValueError:
        raise DeliveryError("rendered output is not a supported image") from None

    delivery_state = current_llm_chat_delivery()
    if delivery_state is not None:
        delivery_state = reserve_media_message()
    await send_with_delivery(session, MessageChain([image]), delivery_state, media=True)
    try:
        await append_history(session.channel.id, "", "bot", "assistant", _IMAGE_HISTORY_MARKER)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        warn(f"{tool_name} delivery history failed: {type(exc).__name__}")
    return success_message or (
        "Rendered image sent successfully. Do not repeat its content in the final response; "
        "return [END_OF_RESPONSE] when no supplement is needed."
    )


async def render_and_deliver(
    session: Session,
    runtime: RenderToolContext,
    operation: RenderCall,
    *,
    tool_name: str,
    width: int,
) -> str:
    """Render, validate, send, and persist one confirmed image delivery."""

    from entari_plugin_htmlrender import HtmlRenderError

    options = render_options(width)
    try:
        renderer = runtime.get_renderer()
        rendered = await operation(renderer, options, runtime.timeout_seconds)
    except asyncio.CancelledError:
        raise
    except HtmlRenderError as exc:
        runtime.warn(f"{tool_name} render failed: {type(exc).__name__}")
        raise DeliveryError("rendering failed or exceeded the configured limits") from None
    except Exception as exc:
        runtime.warn(f"{tool_name} render failed unexpectedly: {type(exc).__name__}")
        raise DeliveryError("the rendering service is unavailable") from None

    return await deliver_image_bytes(
        session,
        bytes(rendered),
        append_history=runtime.append_history,
        warn=runtime.warn,
        tool_name=tool_name,
    )
