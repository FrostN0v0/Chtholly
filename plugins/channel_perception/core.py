"""Pure normalization helpers for channel perception."""

from __future__ import annotations

import re
from dataclasses import dataclass
from collections.abc import Iterable, Sequence

from satori.element import At, File, Text, Audio, Image, Quote, Video, Element

_MEDIA_MARKERS = {
    Image: "[Image]",
    Audio: "[Audio]",
    Video: "[Video]",
    File: "[File]",
}
_WHITESPACE_RE = re.compile(r"\s+")
MAX_NORMALIZED_CONTENT_CHARS = 10_000


@dataclass(frozen=True, slots=True)
class NormalizedMessage:
    content: str
    reply_to_message_id: str
    image_count: int


def clean_text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def display_name(group_card: str, platform_nickname: str, fallback: str) -> str:
    return clean_text(group_card) or clean_text(platform_nickname) or clean_text(fallback)


def collect_image_sources(elements: Iterable[Element]) -> list[str]:
    """Collect non-empty image sources in message order without persisting them."""
    sources: list[str] = []
    for element in elements:
        if isinstance(element, Quote):
            continue
        if isinstance(element, Image):
            source = clean_text(element.src)
            if source:
                sources.append(source)
        if element.children:
            sources.extend(collect_image_sources(element.children))
    return sources


def is_prefixed_command(text: str, prefixes: Sequence[str], nickname: str) -> bool:
    stripped = text.lstrip()
    if any(prefix and stripped.startswith(prefix) for prefix in prefixes):
        return True
    name = nickname.strip()
    return bool(name and re.match(rf"^@?{re.escape(name)}[，,:\s]+", stripped))


def _render_elements(
    elements: Iterable[Element],
    text_parts: list[str],
) -> tuple[str, int]:
    reply_to = ""
    image_count = 0
    for element in elements:
        if isinstance(element, Text):
            text_parts.append(element.text)
            continue
        if isinstance(element, At):
            label = clean_text(element.name) or "member"
            text_parts.append(f"@{label}")
            continue
        if isinstance(element, Quote):
            reply_to = clean_text(element.id) or reply_to
            text_parts.append("[Reply]")
            continue
        marker = next((value for cls, value in _MEDIA_MARKERS.items() if isinstance(element, cls)), None)
        if marker is not None:
            text_parts.append(marker)
            if isinstance(element, Image):
                image_count += 1
            continue
        if element.children:
            nested_reply, nested_images = _render_elements(element.children, text_parts)
            reply_to = nested_reply or reply_to
            image_count += nested_images
    return reply_to, image_count


def normalize_message(elements: Iterable[Element], *, max_chars: int) -> NormalizedMessage:
    text_parts: list[str] = []
    reply_to, image_count = _render_elements(elements, text_parts)
    content = _WHITESPACE_RE.sub(" ", " ".join(text_parts)).strip()
    limit = min(MAX_NORMALIZED_CONTENT_CHARS, max(1, int(max_chars)))
    if len(content) > limit:
        content = "." * limit if limit <= 3 else f"{content[: limit - 3].rstrip()}..."
    return NormalizedMessage(
        content=content,
        reply_to_message_id=reply_to,
        image_count=image_count,
    )
