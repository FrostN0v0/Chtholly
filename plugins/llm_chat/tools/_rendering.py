"""Shared runtime and preparation primitives for rendered-image tools."""

from __future__ import annotations

from typing import TYPE_CHECKING
import asyncio
from pathlib import Path
from dataclasses import dataclass
from collections.abc import Callable, Awaitable

from arclet.entari import Image, Session

from ..core.types import JSONType
from ..core.delivery import DeliveryError
from ..prepared_media import prepare_media
from ..core.image_source import IMAGE_FETCH_MAX_BYTES

if TYPE_CHECKING:
    from entari_plugin_htmlrender import HtmlRenderer, RasterOptions, RenderedImage

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


async def prepare_image_bytes(
    session: Session,
    data: bytes | bytearray | memoryview,
    *,
    warn: WarningSink,
    tool_name: str,
    edited: bool = False,
) -> dict[str, JSONType]:
    """Validate and register one image for explicit composition with send_msg."""

    raw = bytes(data)
    if not raw or len(raw) > IMAGE_FETCH_MAX_BYTES:
        raise DeliveryError("rendered image is empty or exceeds the delivery size limit")
    try:
        image = Image.of(raw=raw)
    except ValueError:
        raise DeliveryError("rendered output is not a supported image") from None
    if image.src[5:].partition(";")[0] not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
        raise DeliveryError("rendered output is not a supported image")

    return prepare_media(
        session,
        image,
        byte_count=len(raw),
        tool_name=tool_name,
        history_marker=_IMAGE_HISTORY_MARKER,
        edited=edited,
    )


async def render_and_prepare(
    session: Session,
    runtime: RenderToolContext,
    operation: RenderCall,
    *,
    tool_name: str,
    width: int,
) -> dict[str, JSONType]:
    """Render and validate one image without sending or changing visible history."""

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

    return await prepare_image_bytes(
        session,
        bytes(rendered),
        warn=runtime.warn,
        tool_name=tool_name,
    )
