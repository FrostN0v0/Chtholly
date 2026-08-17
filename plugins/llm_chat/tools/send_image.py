"""send_image LLM tool implementation."""

from __future__ import annotations

from typing import cast
import asyncio
from pathlib import Path
from collections import deque
from dataclasses import field, dataclass
from collections.abc import Callable, Sequence, Awaitable

from arclet.entari import Image, Session, MessageChain
from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from ..config import LLMChatConfig
from ..models import ImageTag
from ._delivery import send_with_delivery
from ..core.types import JSONType
from ._registration import register_tool
from ..core.delivery import DeliveryError, reserve_media_messages, current_llm_chat_delivery
from ._image_catalog import (
    ImageCatalog,
    find_image_row,
    find_explicit_image_row,
    normalize_image_reference,
)
from ..core.image_source import image_file_to_data_url

ImagePicker = Callable[[LLMChatConfig, Sequence[ImageTag], str, deque[str]], Awaitable[str | None]]
HistoryAppender = Callable[[str, str, str, str, str], Awaitable[object]]


@dataclass
class ImageToolContext:
    """Mutable dependencies and recent-selection state for image tools."""

    config: LLMChatConfig
    catalog: ImageCatalog
    pick_image: ImagePicker
    append_history: HistoryAppender
    warn: Callable[[str], object]
    recent_window: int = 5
    recent_images: dict[str, deque[str]] = field(default_factory=dict)


def register_send_image(
    dispatcher: PluginDispatcher[JSONType],
    runtime: ImageToolContext,
) -> Subscriber[JSONType]:
    """Register local registered-image delivery."""

    async def send_image(
        session: Session,
        context: str = "",
        image_paths: list[str] = cast(list[str], None),
    ) -> str:
        """Send registered local reaction images or stickers.

        Provide compact emotion, scenario, and subject keywords in context for one semantic match. When
        list_image_resources returns exact registered relative paths, provide them through image_paths to send one or
        multiple images in order. Exact paths are internal tool data and must never be revealed to the user. Provide
        exactly one selection mode: non-empty context or non-empty image_paths. Duplicate paths are sent once.
        Use proactively for explicit requests and natural emotional reactions in casual conversation. Examples
        include greetings, teasing, embarrassment, affection, comfort, celebration, surprise, jealousy,
        exasperation, or light complaints. Do not wait for an explicit sticker request when a fitting image would
        express the tone more naturally. This is not image generation, web search, or analysis of an attached image.

        Args:
            context (str): Compact emotion/scenario tags or one exact registered relative path. Defaults to empty.
            image_paths (list[str] | None): Exact registered relative paths to send in order. Defaults to none.
        Returns:
            str: Sanitized delivery result without paths, tags, hashes, or database details.
        """

        normalized_context = context.strip() if isinstance(context, str) else ""
        paths_provided = bool(image_paths)
        if bool(normalized_context) == paths_provided:
            raise DeliveryError("Provide exactly one of context or image_paths")

        rows = await runtime.catalog.load_rows()
        if not rows:
            return "没有可用的图片"

        recent = runtime.recent_images.setdefault(
            session.channel.id,
            deque(maxlen=runtime.recent_window),
        )
        selected: list[tuple[ImageTag, Path]] = []
        if paths_provided:
            if not isinstance(image_paths, list) or not image_paths:
                raise DeliveryError("Registered image path is unavailable")
            seen: set[str] = set()
            for value in image_paths:
                if not isinstance(value, str):
                    raise DeliveryError("Registered image path is unavailable")
                normalized = normalize_image_reference(value)
                if not normalized or normalized in seen:
                    continue
                row = find_image_row(rows, value)
                if row is None:
                    raise DeliveryError("Registered image path is unavailable")
                full = runtime.catalog.resolve(row.file_path)
                if full is None or not full.is_file():
                    raise DeliveryError("Registered image path is unavailable")
                seen.add(normalized)
                selected.append((row, full))
            if not selected:
                raise DeliveryError("Registered image path is unavailable")
        else:
            row = find_explicit_image_row(rows, normalized_context)
            if row is None:
                relative_path = await runtime.pick_image(runtime.config, rows, normalized_context, recent)
                if relative_path is None:
                    return "没有合适的图片"
                row = find_image_row(rows, relative_path)
            if row is None:
                return "图片标签记录已丢失"
            full = runtime.catalog.resolve(row.file_path)
            if full is None or not full.is_file():
                return "图片文件已丢失"
            selected.append((row, full))
        prepared: list[tuple[ImageTag, Image]] = []
        for row, full in selected:
            data_url = image_file_to_data_url(full)
            if data_url is None:
                raise DeliveryError("Registered image file is unreadable, invalid, or too large")
            prepared.append((row, Image.of(url=data_url)))

        delivery_state = current_llm_chat_delivery()
        if delivery_state is not None:
            delivery_state = reserve_media_messages(len(prepared))

        total = len(prepared)
        for index, (row, image) in enumerate(prepared):
            try:
                await send_with_delivery(
                    session,
                    MessageChain([image]),
                    delivery_state,
                    media=True,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                if index:
                    raise DeliveryError(
                        f"image delivery confirmed {index}/{total} images before failure; "
                        "do not repeat the confirmed prefix"
                    ) from None
                raise
            recent.append(row.file_path)
            tag_hint = "，".join(row.tags.split("，")[:5])
            try:
                await runtime.append_history(
                    session.channel.id,
                    "",
                    "bot",
                    "assistant",
                    f"[发送了表情包: {tag_hint}]",
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                runtime.warn(f"image delivery history failed: {type(exc).__name__}")

        if paths_provided:
            return f"已发送 {total} 张图片；不要在最终回复中重复，若无需补充只返回 [END_OF_RESPONSE]。"
        return f"已发送图片（{normalized_context}）"

    return register_tool(dispatcher, send_image)
