"""Bounded, quoted channel context without persistent message capabilities."""

from __future__ import annotations

import json
from typing import cast
from dataclasses import dataclass
from collections.abc import Mapping, Sequence

from .types import ChatMessage

MAX_CHANNEL_CONTEXT_CHARS = 12_000
_CONTEXT_FIELDS = (
    "participant_ref",
    "display_name",
    "content",
    "image_count",
    "created_at",
    "minutes_ago",
    "directed_to_bot",
    "is_bot",
    "mentions",
)


@dataclass(frozen=True, slots=True)
class ChannelContextSnapshot:
    model_payload: dict[str, object]

    @property
    def provenance(self) -> dict[str, object]:
        messages = cast(list[dict[str, object]], self.model_payload["messages"])
        return {
            "status": self.model_payload["status"],
            "requested_messages": self.model_payload["requested_messages"],
            "included_messages": len(messages),
            "truncated": self.model_payload["truncated"],
        }


def build_channel_context(
    messages: Sequence[Mapping[str, object]], *, requested_messages: int
) -> ChannelContextSnapshot:
    """Keep the newest contiguous readable window and recalculate visible replies."""

    def encode(selected: Sequence[Mapping[str, object]]) -> dict[str, object]:
        positions = {str(item["cursor"]): index for index, item in enumerate(selected, start=1)}
        public: list[dict[str, object]] = []
        for item in selected:
            projected = {key: item[key] for key in _CONTEXT_FIELDS if key in item}
            target = str(item.get("reply_to_cursor", ""))
            projected["reply_to_status"] = (
                ("available" if target in positions else "outside_context")
                if target
                else str(item.get("reply_to_status", "none"))
            )
            if target in positions:
                projected["reply_to_index"] = positions[target]
            public.append(projected)
        return {
            "status": "available" if messages else "empty",
            "requested_messages": requested_messages,
            "messages": public,
            "truncated": len(selected) < len(messages),
        }

    selected: list[Mapping[str, object]] = []
    for item in reversed(messages[-requested_messages:]):
        candidate = [item, *selected]
        if len(json.dumps(encode(candidate), ensure_ascii=False, separators=(",", ":"))) > MAX_CHANNEL_CONTEXT_CHARS:
            break
        selected = candidate
    return ChannelContextSnapshot(encode(selected))


def unavailable_channel_context(requested_messages: int) -> ChannelContextSnapshot:
    """Unavailable collection is not evidence that the channel was quiet."""
    return ChannelContextSnapshot(
        {"status": "unavailable", "requested_messages": requested_messages, "messages": [], "truncated": False}
    )


def attach_channel_context(message: ChatMessage, snapshot: ChannelContextSnapshot) -> ChatMessage:
    """Add quoted data to the structured user text, preserving every media part."""
    content = message["content"]
    if isinstance(content, str):
        encoded = content
    elif isinstance(content, list) and content and content[0].get("type") == "text":
        encoded = content[0]["text"]
    else:
        raise ValueError("Channel context requires a structured user text part")
    payload = json.loads(encoded)
    if not isinstance(payload, dict):
        raise ValueError("Channel context requires an object user payload")
    payload["recent_channel_context"] = snapshot.model_payload
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    updated = text if isinstance(content, str) else [{**content[0], "text": text}, *content[1:]]
    return cast(ChatMessage, {**message, "content": updated})
