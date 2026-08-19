"""Structured metadata for text-aware reaction-image retrieval."""

from __future__ import annotations

import re
import json
from typing import Literal
from dataclasses import dataclass
from collections.abc import Mapping, Sequence

from .media import normalize_image_tags

ImageTagFormat = Literal["structured", "legacy", "empty"]

_TEXT_LIMIT = 120
_MEANING_LIMIT = 160
_SCENARIO_LIMIT = 90
_TAG_LIMIT = 40
_MAX_SCENARIOS = 4
_MAX_TAGS = 12
_EMPTY_VALUES = frozenset({"", "无", "没有", "none", "null", "n/a"})
_MATCH_NORMALIZE_RE = re.compile(r"[\W_]+", re.UNICODE)
_LIST_SPLIT_RE = re.compile(r"[，,、;；\n\r]+")


@dataclass(frozen=True, slots=True)
class ImageTagMetadata:
    """Validated image meaning and retrieval constraints."""

    text: str
    meaning: str
    use_when: tuple[str, ...]
    avoid_when: tuple[str, ...]
    tags: tuple[str, ...]

    @property
    def has_visible_text(self) -> bool:
        return bool(self.text)

    def to_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "meaning": self.meaning,
            "use_when": list(self.use_when),
            "avoid_when": list(self.avoid_when),
            "tags": list(self.tags),
        }


def _clean_scalar(value: object, *, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = " ".join(value.split()).strip()
    if cleaned.casefold() in _EMPTY_VALUES:
        return ""
    return cleaned[:limit]


def _clean_items(
    value: object,
    *,
    count: int,
    item_limit: int,
    split_nested: bool = False,
) -> tuple[str, ...]:
    raw_items: list[object] = []
    if isinstance(value, str):
        raw_items.extend(_LIST_SPLIT_RE.split(value))
    elif isinstance(value, Sequence):
        for raw in value:
            if split_nested and isinstance(raw, str):
                raw_items.extend(_LIST_SPLIT_RE.split(raw))
            else:
                raw_items.append(raw)
    else:
        return ()

    items: list[str] = []
    seen: set[str] = set()
    for raw in raw_items:
        item = _clean_scalar(raw, limit=item_limit)
        if not item or item in seen:
            continue
        seen.add(item)
        items.append(item)
        if len(items) >= count:
            break
    return tuple(items)


def _extract_mapping(value: str) -> Mapping[str, object] | None:
    start = value.find("{")
    if start < 0:
        return None
    try:
        parsed, _end = json.JSONDecoder().raw_decode(value[start:])
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, Mapping):
        return None
    return parsed


def _metadata_from_mapping(value: Mapping[str, object]) -> ImageTagMetadata | None:
    metadata = ImageTagMetadata(
        text=_clean_scalar(value.get("text"), limit=_TEXT_LIMIT),
        meaning=_clean_scalar(value.get("meaning"), limit=_MEANING_LIMIT),
        use_when=_clean_items(
            value.get("use_when"),
            count=_MAX_SCENARIOS,
            item_limit=_SCENARIO_LIMIT,
        ),
        avoid_when=_clean_items(
            value.get("avoid_when"),
            count=_MAX_SCENARIOS,
            item_limit=_SCENARIO_LIMIT,
        ),
        tags=_clean_items(
            value.get("tags"),
            count=_MAX_TAGS,
            item_limit=_TAG_LIMIT,
            split_nested=True,
        ),
    )
    if not any((metadata.text, metadata.meaning, metadata.use_when, metadata.tags)):
        return None
    return metadata


def _metadata_from_legacy(value: str) -> ImageTagMetadata | None:
    normalized = normalize_image_tags(value, limit=_MAX_TAGS)
    tags = _clean_items(normalized, count=_MAX_TAGS, item_limit=_TAG_LIMIT)
    if not tags:
        return None
    return ImageTagMetadata(text="", meaning="", use_when=(), avoid_when=(), tags=tags)


def parse_image_tag_metadata(value: str) -> ImageTagMetadata | None:
    """Parse stored structured metadata; legacy comma tags return ``None``."""

    mapping = _extract_mapping(value.strip())
    return _metadata_from_mapping(mapping) if mapping is not None else None


def normalize_generated_image_tags(value: str) -> str:
    """Normalize vision or manual input to canonical compact JSON."""

    stripped = value.strip()
    metadata = parse_image_tag_metadata(stripped)
    if metadata is None:
        if "{" in stripped:
            return ""
        metadata = _metadata_from_legacy(stripped)
    if metadata is None:
        return ""
    return json.dumps(metadata.to_dict(), ensure_ascii=False, separators=(",", ":"))


def image_tag_format(value: str) -> ImageTagFormat:
    if not value.strip():
        return "empty"
    return "structured" if parse_image_tag_metadata(value) is not None else "legacy"


def image_tag_metadata_payload(value: str, *, coerce_legacy: bool = False) -> dict[str, object] | None:
    metadata = parse_image_tag_metadata(value)
    if metadata is None and coerce_legacy:
        metadata = _metadata_from_legacy(value)
    return metadata.to_dict() if metadata is not None else None


def image_tag_display_tags(value: str) -> tuple[str, ...]:
    metadata = parse_image_tag_metadata(value)
    if metadata is not None:
        return metadata.tags
    legacy = _metadata_from_legacy(value)
    return legacy.tags if legacy is not None else ()


def image_tag_embedding_text(value: str) -> str:
    """Build positive-only text for semantic embedding."""

    metadata = parse_image_tag_metadata(value)
    if metadata is None:
        return value
    parts: list[str] = []
    if metadata.text:
        parts.append(f"图片文字：{metadata.text}")
    if metadata.meaning:
        parts.append(f"表达含义：{metadata.meaning}")
    if metadata.use_when:
        parts.append(f"适用场景：{'；'.join(metadata.use_when)}")
    if metadata.tags:
        parts.append(f"情绪标签：{'、'.join(metadata.tags)}")
    return "。".join(parts)


def image_tag_search_text(value: str) -> str:
    """Build positive lexical terms for IDF fallback matching."""

    metadata = parse_image_tag_metadata(value)
    if metadata is None:
        return value
    terms = [metadata.text, metadata.meaning, *metadata.use_when, *metadata.tags]
    return "，".join(term for term in terms if term)


def image_tag_catalog_summary(value: str) -> str:
    """Render structured metadata for internal catalog consumers."""

    metadata = parse_image_tag_metadata(value)
    if metadata is None:
        return value
    parts: list[str] = []
    if metadata.text:
        parts.append(f"原文：{metadata.text}")
    if metadata.meaning:
        parts.append(f"含义：{metadata.meaning}")
    if metadata.use_when:
        parts.append(f"适用：{'、'.join(metadata.use_when)}")
    if metadata.avoid_when:
        parts.append(f"避用：{'、'.join(metadata.avoid_when)}")
    if metadata.tags:
        parts.append(f"标签：{'、'.join(metadata.tags)}")
    return "；".join(parts)


def image_tag_history_hint(value: str, *, limit: int = 5) -> str:
    """Render a short internal successful-delivery marker hint."""

    metadata = parse_image_tag_metadata(value)
    if metadata is None:
        return "，".join(value.split("，")[:limit])
    terms = [metadata.meaning, *metadata.tags]
    return "，".join(term for term in terms if term)[:160]


def image_tag_has_visible_text(value: str) -> bool:
    metadata = parse_image_tag_metadata(value)
    return metadata.has_visible_text if metadata is not None else False


def _normalize_match_text(value: str) -> str:
    return _MATCH_NORMALIZE_RE.sub("", value.casefold())


def image_tag_avoids_context(value: str, context: str) -> bool:
    """Reject a structured image when one concrete avoid phrase matches."""

    metadata = parse_image_tag_metadata(value)
    if metadata is None or not metadata.avoid_when:
        return False
    normalized_context = _normalize_match_text(context)
    if len(normalized_context) < 2:
        return False
    for term in metadata.avoid_when:
        normalized_term = _normalize_match_text(term)
        if len(normalized_term) < 2:
            continue
        if normalized_term in normalized_context or normalized_context in normalized_term:
            return True
    return False
