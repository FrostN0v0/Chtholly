"""Registered reaction image preparation."""

from __future__ import annotations

from typing import cast
from hashlib import sha256
from pathlib import Path
from collections import deque
from dataclasses import field, dataclass
from collections.abc import Callable, Sequence, Awaitable

from arclet.entari import Image, Session
from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from ..config import LLMChatConfig
from ..models import ImageTag
from ..core.types import JSONType
from ._registration import register_tool
from ..core.delivery import DeliveryError
from ._image_catalog import (
    ImageCatalog,
    find_image_row,
    find_explicit_image_row,
    normalize_image_reference,
)
from ..prepared_media import prepare_media, ensure_media_capacity, prepared_media_metadata
from ..core.tool_trace import record_tool_evidence
from ..core.image_source import IMAGE_FETCH_MAX_BYTES
from ..core.image_tag_metadata import image_tag_history_hint, parse_image_tag_metadata

ImagePicker = Callable[[LLMChatConfig, Sequence[ImageTag], str, deque[str]], Awaitable[str | None]]


@dataclass
class ImageToolContext:
    """Mutable dependencies and recent-selection state for image tools."""

    config: LLMChatConfig
    catalog: ImageCatalog
    pick_image: ImagePicker
    warn: Callable[[str], object]
    recent_window: int = 5
    recent_images: dict[str, deque[str]] = field(default_factory=dict)


def register_prepare_image(
    dispatcher: PluginDispatcher[JSONType],
    runtime: ImageToolContext,
) -> Subscriber[JSONType]:
    """Register local registered-image preparation."""

    async def prepare_image(
        session: Session,
        context: str = "",
        image_paths: list[str] = cast(list[str], None),
    ) -> JSONType:
        """Prepare registered local reaction images or stickers without sending them.

        Provide compact positive emotion, scenario, and subject keywords in context for one semantic match. Never add
        negations, exclusions, directory names, or internal paths to context. When list_image_resources returns
        registered resources, provide exact registered relative paths through image_paths.
        Prepare multiple images in order. Exact paths are internal tool data and must never be revealed to the user.
        Provide exactly one selection mode: non-empty context or non-empty image_paths. Duplicate paths prepare once.
        Use proactively for explicit requests and natural emotional reactions in casual conversation. Examples
        include greetings, teasing, embarrassment, affection, comfort, celebration, surprise, jealousy,
        exasperation, or light complaints. Do not wait for an explicit sticker request when a fitting image would
        express the tone more naturally. This is not image generation, web search, or analysis of an attached image.

        Args:
            context (str): Compact emotion/scenario tags or one exact registered relative path. Defaults to empty.
            image_paths (list[str] | None): Exact registered relative paths to prepare in order. Defaults to none.
        Returns:
            dict | str: Prepared reference, ordered media entries, or a safe no-match result. Use send_msg to send.
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
                pending_keys = {
                    item.get("catalog_key") for item in prepared_media_metadata() if item.get("catalog_key")
                }
                candidates = [
                    item for item in rows if sha256(item.file_path.encode("utf-8")).hexdigest() not in pending_keys
                ]
                relative_path = await runtime.pick_image(runtime.config, candidates, normalized_context, recent)
                if relative_path is None:
                    return "没有合适的图片"
                row = find_image_row(rows, relative_path)
            if row is None:
                return "图片标签记录已丢失"
            full = runtime.catalog.resolve(row.file_path)
            if full is None or not full.is_file():
                return "图片文件已丢失"
            selected.append((row, full))
        prepared: list[tuple[ImageTag, Image, int]] = []
        for row, full in selected:
            try:
                with full.open("rb") as source:
                    data = source.read(IMAGE_FETCH_MAX_BYTES + 1)
                if not data or len(data) > IMAGE_FETCH_MAX_BYTES:
                    raise ValueError
                image = Image.of(raw=data)
                if image.src[5:].partition(";")[0] not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
                    raise ValueError
            except (OSError, ValueError):
                raise DeliveryError("Registered image file is unreadable, invalid, or too large") from None
            prepared.append((row, image, len(data)))

        ensure_media_capacity(len(prepared), byte_count=sum(size for _, _, size in prepared))
        entries: list[JSONType] = []
        for row, image, byte_count in prepared:

            async def on_confirm(row: ImageTag = row) -> None:
                recent.append(row.file_path)
                metadata = parse_image_tag_metadata(row.tags)
                record_tool_evidence(
                    {
                        "images": [
                            {
                                "path": row.file_path.replace("\\", "/"),
                                "meaning": metadata.meaning if metadata is not None else "",
                                "text": metadata.text if metadata is not None else "",
                            }
                        ]
                    }
                )

            entry = prepare_media(
                session,
                image,
                byte_count=byte_count,
                tool_name="prepare_image",
                history_marker=f"[发送了表情包: {image_tag_history_hint(row.tags)}]",
                on_confirm=on_confirm,
                metadata={"catalog_key": sha256(row.file_path.encode("utf-8")).hexdigest()},
            )
            entries.append(entry)
        if paths_provided:
            return {"status": "prepared", "media": entries}
        return entries[0]

    return register_tool(dispatcher, prepare_image)
