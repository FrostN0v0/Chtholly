"""Host-owned speaker attribution for structured user turns."""

from __future__ import annotations

import json
from typing import cast
from hashlib import sha256
from collections.abc import Mapping, Callable

from .types import ChatMessage

_HOST_ATTRIBUTION_KEYS = frozenset({"speaker", "speaker_id", "previous_turn"})


def derive_speaker_id(scope_ref: str, user_id: str) -> str:
    """Return a scope-stable member label that grants nothing and does not reveal the unified user ID."""
    digest = sha256(f"{scope_ref}\x00{user_id}".encode()).hexdigest()
    return f"m_{digest[:10]}"


def describe_previous_turn(speaker: str, previous_speaker_id: str, current_speaker_id: str) -> dict[str, object]:
    return {
        "speaker": speaker,
        "speaker_id": previous_speaker_id,
        "same_speaker": previous_speaker_id == current_speaker_id,
    }


def update_user_payload(
    message: ChatMessage,
    update: Callable[[dict[str, object]], dict[str, object]],
) -> ChatMessage:
    """Rewrite the structured JSON text of one user message while preserving every media part."""
    content = message["content"]
    if isinstance(content, str):
        encoded = content
    elif isinstance(content, list) and content and content[0].get("type") == "text":
        encoded = content[0]["text"]
    else:
        raise ValueError("A structured user message requires a leading text part")
    payload = json.loads(encoded)
    if not isinstance(payload, dict):
        raise TypeError("A structured user message requires an object payload")
    text = json.dumps(update(payload), ensure_ascii=False, separators=(",", ":"))
    updated = text if isinstance(content, str) else [{**content[0], "text": text}, *content[1:]]
    return cast(ChatMessage, {**message, "content": updated})


def attribute_user_payload(
    payload: Mapping[str, object],
    *,
    speaker_id: str,
    fallback_speaker: str,
    previous_turn: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Put host attribution first; payload fields can never replace the author identity."""
    speaker = payload.get("speaker")
    attributed: dict[str, object] = {
        "speaker": speaker if isinstance(speaker, str) and speaker else fallback_speaker or "member",
        "speaker_id": speaker_id,
    }
    if previous_turn is not None:
        attributed["previous_turn"] = dict(previous_turn)
    attributed.update((key, value) for key, value in payload.items() if key not in _HOST_ATTRIBUTION_KEYS)
    return attributed


def attribute_stored_user_content(content: str, *, speaker_id: str, fallback_speaker: str) -> str:
    """Project one persisted user input, including legacy plain text, into the attributed JSON protocol."""
    try:
        payload = json.loads(content)
    except ValueError:
        payload = None
    if not isinstance(payload, dict) or "content" not in payload:
        payload = {"content": content}
    attributed = attribute_user_payload(payload, speaker_id=speaker_id, fallback_speaker=fallback_speaker)
    return json.dumps(attributed, ensure_ascii=False, separators=(",", ":"))
