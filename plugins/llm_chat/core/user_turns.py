"""Host-owned speaker attribution for structured user turns."""

from __future__ import annotations

import json
from typing import cast
from hashlib import sha256
from collections.abc import Mapping, Callable

from .types import ChatMessage

_HOST_ATTRIBUTION_KEYS = frozenset({"speaker", "speaker_id", "previous_turn"})
_COMPANION_PRIVATE_FIELDS = frozenset(
    {
        "host_author",
        "own_text",
        "companion_participant_context",
        "user_id",
        "platform_user_id",
        "participant_ref",
        "participant_refs",
        "current_participant_ref",
        "source_message_id",
        "source_message_ids",
        "message_id",
        "image_ref",
        "media_ref",
        "reply_to_ref",
        "evidence_turn_ids",
        "processed_turn_id",
    }
)


def companion_history_payload(value: object) -> object:
    """Keep conversation data, never stale profile/identity capabilities or audit metadata."""
    if isinstance(value, Mapping):
        return {
            key: companion_history_payload(item) for key, item in value.items() if key not in _COMPANION_PRIVATE_FIELDS
        }
    if isinstance(value, list):
        return [companion_history_payload(item) for item in value]
    return value


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


def event_user_author(payload: Mapping[str, object], *, speaker_id: str, fallback_speaker: str) -> tuple[str, str]:
    """Use only host event metadata, never author claims inside user content."""
    author = payload.get("host_author")
    if isinstance(author, Mapping) and author.get("version") == 1:
        member, name = author.get("speaker_id"), author.get("display_name")
        if isinstance(member, str) and member and isinstance(name, str):
            return member, name or "member"
    return speaker_id, fallback_speaker


def project_event_user_content(
    payload: Mapping[str, object], *, speaker_id: str, fallback_speaker: str, companion_history: bool = False
) -> str | None:
    content = payload.get("content")
    if not isinstance(content, str) or not content:
        return None
    member, name = event_user_author(payload, speaker_id=speaker_id, fallback_speaker=fallback_speaker)
    host_bound = "host_author" in payload
    if not host_bound and not companion_history:
        return attribute_stored_user_content(content, speaker_id=member, fallback_speaker=name)
    author = payload.get("host_author")
    if host_bound and (
        not isinstance(author, Mapping)
        or type(author.get("version")) is not int
        or author.get("version") != 1
        or not isinstance(author.get("user_id"), str)
        or not author["user_id"]
        or not isinstance(author.get("speaker_id"), str)
        or not author["speaker_id"]
        or not isinstance(author.get("display_name"), str)
    ):
        return None
    try:
        nested = json.loads(content)
    except ValueError:
        nested = None
    if not isinstance(nested, dict) or "content" not in nested:
        nested = {"content": content}
    nested = cast(dict[str, object], companion_history_payload(nested))
    # Unlike legacy inputs, companion event authors are exact host bindings.
    if host_bound:
        nested["speaker"] = name
    return json.dumps(
        attribute_user_payload(nested, speaker_id=member, fallback_speaker=name),
        ensure_ascii=False,
        separators=(",", ":"),
    )
