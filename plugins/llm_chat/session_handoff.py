"""Structured context-session handoff generation with deterministic fallback."""

from __future__ import annotations

import json
from typing import cast
from collections.abc import Mapping, Sequence

import litellm
from entari_plugin_llm.config import get_model_config

from .models import ChatScope, ContextSession
from .agent_events import load_event_payload, load_session_events
from .context_reads import model_readable_payload
from .core.model_json import load_model_json
from .core.user_turns import derive_speaker_id, event_user_author, project_event_user_content
from .core.model_audit import ADMIN_ONLY_EVENT_TYPES
from .scheduled_chat_context import _user_content, _join_user_inputs

_HANDOFF_KEYS = (
    "topic",
    "goals",
    "decisions",
    "constraints",
    "open_loops",
    "confirmed_deliveries",
    "relevant_event_refs",
    "participants",
    "last_visible_exchange",
)
_ATTRIBUTED_KEYS = ("goals", "decisions", "constraints", "open_loops")
_MODEL_KEYS = tuple(key for key in _HANDOFF_KEYS if key not in {"participants", "last_visible_exchange"})
_GROUP_SUBJECT = "group"
_UNKNOWN_SUBJECT = "unknown"
_MAX_PARTICIPANTS = 20
_HANDOFF_INSTRUCTIONS = (
    "Summarize one completed group chat context session as strict JSON. Use only supplied events. "
    "Several members may share the session: participants maps each speaker_id to its display name, and each "
    "user_input has its own host-authored speaker_id even when several members share one turn_id. Never merge "
    "different members or describe a member's request, promise, emotion or joke as someone else's. "
    "Each item of goals, decisions, "
    'constraints and open_loops must be an object {"subject": speaker_id or "group", "text": string}; use '
    '"group" only for genuinely shared context. Never include hidden reasoning, secrets, raw large payloads or '
    f"unsupported claims. Return exactly these keys: {', '.join(_MODEL_KEYS)}."
)


def _event_summary(event, *, fallback_speaker: str = "user") -> dict[str, object]:
    payload = model_readable_payload(event, compact=event.event_type not in {"user_input", "assistant_output"})
    item: dict[str, object] = {
        "event_ref": event.event_ref,
        "turn_id": event.turn_id,
        "type": event.event_type,
        "status": event.status,
        "effect": event.effect,
    }
    if event.tool_name:
        item["tool"] = event.tool_name
    if event.event_type in {"user_input", "assistant_output"}:
        if event.event_type == "user_input":
            own_input = {**payload, "input_format": load_event_payload(event).get("input_format")}
            speaker, content = _user_content(own_input, fallback_speaker)
            item["speaker"] = speaker
            if "host_author" in load_event_payload(event):
                member, name = event_user_author(load_event_payload(event), speaker_id="", fallback_speaker=speaker)
                item["speaker"], item["speaker_id"] = name, member
        else:
            content = payload.get("content")
        if isinstance(content, str):
            item["content"] = content[:2000]
    elif event.event_type == "assistant_tool_call":
        item["arguments"] = payload.get("arguments", {})
    elif event.event_type == "tool_result":
        item["result"] = payload.get("result", {})
    return item


async def _source_events(context_session: ContextSession, max_chars: int, *, scope_ref: str) -> list[dict[str, object]]:
    if context_session.status == "sealed":
        return []
    rows = await load_session_events(context_session.id, model_visible_only=False, turn_limit=40)
    selected: list[dict[str, object]] = []
    used = 2
    for turn, events in reversed(rows):
        speaker_id = derive_speaker_id(scope_ref, turn.user_id)
        companion_turn = turn.trigger_kind == "autonomous" or any(
            event.event_type == "user_input" and "host_author" in load_event_payload(event) for event in events
        )
        summaries = [
            _event_summary(event, fallback_speaker=turn.user_name)
            for event in events
            if event.event_type not in ADMIN_ONLY_EVENT_TYPES | {"companion_participation"}
            and event.model_visible
            and (not companion_turn or event.event_type != "assistant_output" or event.status == "confirmed")
            and (turn.trigger_kind != "autonomous" or event.event_type in {"user_input", "assistant_output"})
            and (
                event.event_type != "user_input"
                or "host_author" not in load_event_payload(event)
                or project_event_user_content(
                    load_event_payload(event), speaker_id=speaker_id, fallback_speaker=turn.user_name
                )
                is not None
            )
        ]
        for item in summaries:
            if item["type"] == "user_input":
                item.setdefault("speaker_id", speaker_id)
        sizes = [len(json.dumps(item, ensure_ascii=False, separators=(",", ":"))) + 1 for item in summaries]
        required = sum(size for item, size in zip(summaries, sizes) if item["type"] == "user_input")
        if used + required > max_chars:
            continue
        used += required
        for item, size in reversed(list(zip(summaries, sizes))):
            if item["type"] != "user_input":
                if used + size > max_chars:
                    continue
                used += size
            selected.insert(0, item)
    return selected


def _participants(events: Sequence[Mapping[str, object]]) -> dict[str, str]:
    """Host-derived names for the latest speakers; model output never defines identities."""
    names: dict[str, str] = {}
    for item in reversed(events):
        speaker_id, speaker = item.get("speaker_id"), item.get("speaker")
        if item.get("type") != "user_input" or not isinstance(speaker_id, str) or speaker_id in names:
            continue
        names[speaker_id] = str(speaker or "")[:120]
        if len(names) >= _MAX_PARTICIPANTS:
            break
    return dict(reversed(names.items()))


def _last_visible_exchange(events: Sequence[Mapping[str, object]], max_chars: int) -> dict[str, object]:
    """Pair the final member input only with the assistant output of that same turn."""
    visible = [event for event in events if event.get("type") in {"user_input", "assistant_output"}]
    last_user_event = next((item for item in reversed(visible) if item.get("type") == "user_input"), None)
    if last_user_event is None:
        return {"speaker": "", "speaker_id": "", "user": "", "assistant": ""}
    turn_id = last_user_event.get("turn_id")
    user_inputs = [
        str(item.get("content", ""))
        for item in visible
        if item.get("type") == "user_input"
        and item.get("turn_id") == turn_id
        and item.get("speaker_id") == last_user_event.get("speaker_id")
    ]
    assistant = next(
        (
            str(item.get("content", ""))
            for item in reversed(visible)
            if item.get("type") == "assistant_output" and item.get("turn_id") == turn_id
        ),
        "",
    )
    return {
        "speaker": str(last_user_event.get("speaker", "")),
        "speaker_id": str(last_user_event.get("speaker_id", "")),
        "user": _join_user_inputs(user_inputs, max_chars),
        "assistant": assistant[:max_chars],
    }


def _attributed_items(raw: object, speakers: set[str]) -> list[dict[str, str]]:
    values = raw if isinstance(raw, list) else []
    items: list[dict[str, str]] = []
    for value in values[:20]:
        if isinstance(value, Mapping):
            subject, text = str(value.get("subject", "")), str(value.get("text", ""))
        else:
            subject, text = _UNKNOWN_SUBJECT, str(value)
        if not text.strip():
            continue
        if subject != _GROUP_SUBJECT and subject not in speakers:
            subject = _UNKNOWN_SUBJECT
        items.append({"subject": subject, "text": text[:1000]})
    return items


def _normalize_handoff(
    value: object,
    events: Sequence[Mapping[str, object]],
    max_chars: int,
) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    allowed_refs = {str(item["event_ref"]) for item in events}
    participants = _participants(events)
    result: dict[str, object] = {"topic": str(value.get("topic") or "")[:1000]}
    for key in _ATTRIBUTED_KEYS:
        result[key] = _attributed_items(value.get(key), set(participants))
    deliveries = value.get("confirmed_deliveries")
    result["confirmed_deliveries"] = [
        str(item)[:1000] for item in (deliveries if isinstance(deliveries, list) else [])[:20] if str(item).strip()
    ]
    refs = value.get("relevant_event_refs")
    result["relevant_event_refs"] = [
        str(item) for item in (refs if isinstance(refs, list) else []) if str(item) in allowed_refs
    ][:32]
    result["participants"] = participants
    for exchange_chars in (2000, 500):
        result["last_visible_exchange"] = _last_visible_exchange(events, exchange_chars)
        if len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))) <= max_chars:
            return result
    return None


def _fallback_handoff(events: Sequence[Mapping[str, object]], max_chars: int) -> dict[str, object]:
    exchange = _last_visible_exchange(events, 2000)
    confirmed = [
        f"{item.get('tool', 'tool')}:{item['event_ref']}"
        for item in events
        if item.get("type") == "tool_result" and item.get("effect") == "confirmed"
    ][-12:]
    handoff: dict[str, object] = {
        "topic": str(exchange["user"])[:1000],
        "goals": [],
        "decisions": [],
        "constraints": [],
        "open_loops": [],
        "confirmed_deliveries": confirmed,
        "relevant_event_refs": [str(item["event_ref"]) for item in events[-12:]],
        "participants": _participants(events),
        "last_visible_exchange": exchange,
    }
    serialized = json.dumps(handoff, ensure_ascii=False, separators=(",", ":"))
    if len(serialized) <= max_chars:
        return handoff
    handoff["topic"] = ""
    handoff["last_visible_exchange"] = _last_visible_exchange(events, 500)
    return handoff


async def generate_session_handoff(
    context_session: ContextSession,
    *,
    scope: ChatScope,
    model_name: str | None,
    timeout: float,
    source_max_chars: int,
    output_max_chars: int,
) -> str:
    events = await _source_events(context_session, max(1000, source_max_chars), scope_ref=scope.scope_ref)
    if not events:
        return "{}"
    fallback = _fallback_handoff(events, max(1000, output_max_chars))
    source = {"participants": _participants(events), "events": events}
    try:
        conf = get_model_config(model_name, scope.channel_id)
        response = cast(
            litellm.ModelResponse,
            await litellm.acompletion(
                model=conf.name,
                base_url=conf.base_url,
                api_key=conf.api_key,
                timeout=max(1.0, timeout),
                max_retries=0,
                messages=[
                    {"role": "system", "content": _HANDOFF_INSTRUCTIONS},
                    {"role": "user", "content": json.dumps(source, ensure_ascii=False, separators=(",", ":"))},
                ],
            ),
        )
        content = response.choices[0].message.content
        parsed = load_model_json(content) if isinstance(content, str) else None
        handoff = _normalize_handoff(parsed, events, max(1000, output_max_chars)) or fallback
    except Exception:
        handoff = fallback
    return json.dumps(handoff, ensure_ascii=False, separators=(",", ":"))
