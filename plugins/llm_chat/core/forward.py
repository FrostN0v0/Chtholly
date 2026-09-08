"""Pure normalization for inbound OneBot merged-forward payloads."""

from __future__ import annotations

import json
from typing import Literal, TypedDict, cast
from dataclasses import dataclass
from collections.abc import Mapping, Sequence
from typing_extensions import NotRequired

ForwardSource = Literal["direct", "quoted"]
ForwardedSpeakerRole = Literal["assistant", "participant", "unknown"]
ForwardPartKind = Literal[
    "text",
    "image",
    "audio",
    "video",
    "file",
    "emoji",
    "card",
    "reply",
    "forward",
    "unsupported",
]


class ForwardedMessage(TypedDict):
    speaker: str
    content: str
    source: ForwardSource
    speaker_role: NotRequired[ForwardedSpeakerRole]


@dataclass(frozen=True, slots=True)
class ForwardPart:
    kind: ForwardPartKind
    text: str = ""
    source: str | None = None


@dataclass(frozen=True, slots=True)
class ForwardNode:
    speaker: str
    parts: tuple[ForwardPart, ...]


def _as_mapping(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    return cast(Mapping[str, object], value)


def _as_sequence(value: object) -> Sequence[object] | None:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return cast(Sequence[object], value)
    return None


def _string(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    return ""


def _first_text(data: Mapping[str, object], *keys: str) -> str:
    for key in keys:
        value = _string(data.get(key)).strip()
        if value:
            return value
    return ""


def _compact(text: str) -> str:
    return " ".join(text.split())


def _speaker(data: Mapping[str, object] | None) -> str:
    if data is None:
        return "Unknown sender"
    value = _first_text(data, "card", "nickname", "name")
    return _compact(value)[:80] or "Unknown sender"


def _media_source(data: Mapping[str, object]) -> str | None:
    source = _first_text(data, "url", "file", "src")
    return source or None


def _parse_segment(raw: object) -> ForwardPart | None:
    segment = _as_mapping(raw)
    if segment is None:
        return None
    segment_type = _string(segment.get("type")).strip().lower()
    data = _as_mapping(segment.get("data")) or {}

    if segment_type == "text":
        text = _compact(_string(data.get("text")))
        return ForwardPart("text", text=text) if text else None
    if segment_type == "at":
        target = _first_text(data, "name")
        return ForwardPart("text", text=f"@{target}" if target else "@member")
    if segment_type in {"image", "img"}:
        return ForwardPart("image", source=_media_source(data))
    if segment_type in {"record", "audio", "voice"}:
        return ForwardPart("audio", source=_media_source(data))
    if segment_type == "video":
        return ForwardPart("video", source=_media_source(data))
    if segment_type == "file":
        return ForwardPart("file", text=_first_text(data, "name", "file_name", "file"))
    if segment_type in {"face", "emoji"}:
        return ForwardPart("emoji", text=_first_text(data, "name", "id"))
    if segment_type in {"json", "xml", "share", "music"}:
        return ForwardPart("card")
    if segment_type == "reply":
        return ForwardPart("reply")
    if segment_type in {"forward", "node"}:
        return ForwardPart(
            "forward",
            source=_first_text(data, "id", "message_id", "resId", "resid", "m_resid") or None,
        )

    fallback = _compact(_first_text(data, "text", "summary", "name"))
    return ForwardPart("text", text=fallback) if fallback else ForwardPart("unsupported")


def _node_data(raw: object) -> tuple[Mapping[str, object] | None, object, str]:
    item = _as_mapping(raw)
    if item is None:
        return None, (), ""

    if _string(item.get("type")).strip().lower() == "node":
        data = _as_mapping(item.get("data")) or {}
        content = data.get("content", data.get("message", ()))
        return data, content, _string(data.get("raw_message"))

    sender = _as_mapping(item.get("sender"))
    content = item.get("message", item.get("content", ()))
    return sender, content, _string(item.get("raw_message"))


def parse_forward_payload(payload: object) -> list[ForwardNode]:
    """Parse common OneBot get_forward_msg response variants."""
    root = _as_mapping(payload)
    raw_messages = _as_sequence(root.get("messages")) if root is not None else None
    if raw_messages is None:
        return []

    nodes: list[ForwardNode] = []
    for raw_node in raw_messages:
        sender, raw_content, raw_fallback = _node_data(raw_node)
        parts: list[ForwardPart] = []
        segments = _as_sequence(raw_content)
        if segments is not None:
            parts.extend(part for segment in segments if (part := _parse_segment(segment)) is not None)
        elif isinstance(raw_content, str):
            text = _compact(raw_content)
            if text:
                parts.append(ForwardPart("text", text=text))
        if not parts and raw_fallback:
            text = _compact(raw_fallback)
            if text:
                parts.append(ForwardPart("text", text=text))
        nodes.append(ForwardNode(_speaker(sender), tuple(parts)))
    return nodes


def collect_forward_image_sources(nodes: Sequence[ForwardNode]) -> list[str]:
    """Return unique image sources in node order."""
    sources: list[str] = []
    seen: set[str] = set()
    for node in nodes:
        for part in node.parts:
            if part.kind != "image" or not part.source or part.source in seen:
                continue
            seen.add(part.source)
            sources.append(part.source)
    return sources


def collect_nested_forward_ids(nodes: Sequence[ForwardNode]) -> list[str]:
    """Return unique nested merged-forward ids in node order."""
    ids: list[str] = []
    seen: set[str] = set()
    for node in nodes:
        for part in node.parts:
            if part.kind != "forward" or not part.source or part.source in seen:
                continue
            seen.add(part.source)
            ids.append(part.source)
    return ids


def _render_part(part: ForwardPart, image_descriptions: Mapping[str, str]) -> str:
    if part.kind == "text":
        return part.text
    if part.kind == "image":
        description = image_descriptions.get(part.source or "", "").strip()
        return f"[Image: {description}]" if description else "[Image]"
    if part.kind == "audio":
        return "[Audio]"
    if part.kind == "video":
        return "[Video]"
    if part.kind == "file":
        return f"[File: {part.text}]" if part.text else "[File]"
    if part.kind == "emoji":
        return f"[Emoji: {part.text}]" if part.text else "[Emoji]"
    if part.kind == "card":
        return "[Card message]"
    if part.kind == "reply":
        return "[Reply reference]"
    if part.kind == "forward":
        return "[Nested merged forward]"
    return "[Unsupported message]"


def render_forward_node(
    node: ForwardNode,
    *,
    source: ForwardSource,
    image_descriptions: Mapping[str, str],
    max_chars: int,
) -> ForwardedMessage:
    """Render one normalized node into bounded model context."""
    rendered = _compact(" ".join(_render_part(part, image_descriptions) for part in node.parts))
    content = rendered or "[Empty forwarded message]"
    limit = max(32, max_chars)
    if len(content) > limit:
        content = f"{content[: limit - 1]}…"
    return {"speaker": node.speaker, "content": content, "source": source}


def render_forwarded_storage(content: str, forwarded_messages: Sequence[ForwardedMessage]) -> str:
    """Persist outer text and quoted forwarded data without losing attribution."""
    if not forwarded_messages:
        return content
    return json.dumps(
        {"content": content, "forwarded_messages": list(forwarded_messages)},
        ensure_ascii=False,
        separators=(",", ":"),
    )
