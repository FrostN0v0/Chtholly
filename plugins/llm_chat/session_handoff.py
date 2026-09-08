"""Structured context-session handoff generation with deterministic fallback."""

from __future__ import annotations

import json
from typing import cast
from collections.abc import Mapping

import litellm
from entari_plugin_llm.config import get_model_config

from .models import ContextSession
from .agent_events import load_event_payload, load_session_events

_HANDOFF_KEYS = (
    "topic",
    "goals",
    "decisions",
    "constraints",
    "open_loops",
    "confirmed_deliveries",
    "relevant_event_refs",
    "last_visible_exchange",
)


def _event_summary(event) -> dict[str, object]:
    payload = load_event_payload(event)
    item: dict[str, object] = {
        "event_ref": event.event_ref,
        "type": event.event_type,
        "status": event.status,
        "effect": event.effect,
    }
    if event.tool_name:
        item["tool"] = event.tool_name
    if event.event_type in {"user_input", "assistant_output"}:
        content = payload.get("content")
        if isinstance(content, str):
            item["content"] = content[:2000]
    elif event.event_type == "assistant_tool_call":
        item["arguments"] = payload.get("context_arguments", {})
    elif event.event_type == "tool_result":
        item["result"] = payload.get("context_result", {})
    return item


async def _source_events(context_session: ContextSession, max_chars: int) -> list[dict[str, object]]:
    rows = await load_session_events(context_session.id, model_visible_only=False, turn_limit=40)
    selected: list[dict[str, object]] = []
    used = 2
    for _turn, events in reversed(rows):
        for event in reversed(events):
            item = _event_summary(event)
            size = len(json.dumps(item, ensure_ascii=False, separators=(",", ":"))) + 1
            if used + size > max_chars:
                continue
            selected.insert(0, item)
            used += size
    return selected


def _normalize_handoff(value: object, allowed_refs: set[str], max_chars: int) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    result: dict[str, object] = {}
    for key in _HANDOFF_KEYS:
        raw = value.get(key)
        if key == "last_visible_exchange":
            if isinstance(raw, Mapping):
                result[key] = {
                    "user": str(raw.get("user", ""))[:2000],
                    "assistant": str(raw.get("assistant", ""))[:2000],
                }
            else:
                result[key] = {"user": "", "assistant": ""}
            continue
        if key == "topic":
            result[key] = str(raw or "")[:1000]
            continue
        values = raw if isinstance(raw, list) else []
        if key == "relevant_event_refs":
            result[key] = [str(item) for item in values if str(item) in allowed_refs][:32]
        else:
            result[key] = [str(item)[:1000] for item in values[:20] if str(item).strip()]
    serialized = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    return result if len(serialized) <= max_chars else None


def _fallback_handoff(events: list[dict[str, object]], max_chars: int) -> dict[str, object]:
    visible = [event for event in events if event.get("type") in {"user_input", "assistant_output"}]
    last_user = next(
        (str(item.get("content", "")) for item in reversed(visible) if item.get("type") == "user_input"), ""
    )
    last_assistant = next(
        (str(item.get("content", "")) for item in reversed(visible) if item.get("type") == "assistant_output"),
        "",
    )
    confirmed = [
        f"{item.get('tool', 'tool')}:{item['event_ref']}"
        for item in events
        if item.get("type") == "tool_result" and item.get("effect") == "confirmed"
    ][-12:]
    handoff = {
        "topic": last_user[:1000],
        "goals": [],
        "decisions": [],
        "constraints": [],
        "open_loops": [],
        "confirmed_deliveries": confirmed,
        "relevant_event_refs": [str(item["event_ref"]) for item in events[-12:]],
        "last_visible_exchange": {"user": last_user[:2000], "assistant": last_assistant[:2000]},
    }
    serialized = json.dumps(handoff, ensure_ascii=False, separators=(",", ":"))
    if len(serialized) <= max_chars:
        return handoff
    handoff["topic"] = ""
    handoff["last_visible_exchange"] = {"user": last_user[:500], "assistant": last_assistant[:500]}
    return handoff


async def generate_session_handoff(
    context_session: ContextSession,
    *,
    model_name: str | None,
    channel_id: str,
    timeout: float,
    source_max_chars: int,
    output_max_chars: int,
) -> str:
    events = await _source_events(context_session, max(1000, source_max_chars))
    if not events:
        return "{}"
    allowed_refs = {str(item["event_ref"]) for item in events}
    fallback = _fallback_handoff(events, max(1000, output_max_chars))
    try:
        conf = get_model_config(model_name, channel_id)
        response = cast(
            litellm.ModelResponse,
            await litellm.acompletion(
                model=conf.name,
                base_url=conf.base_url,
                api_key=conf.api_key,
                timeout=max(1.0, timeout),
                max_retries=0,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Summarize one completed chat context session as strict JSON. Use only supplied events. "
                            "Never include hidden reasoning, secrets, raw large payloads, or unsupported claims. "
                            f"Return exactly these keys: {', '.join(_HANDOFF_KEYS)}."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(events, ensure_ascii=False, separators=(",", ":")),
                    },
                ],
            ),
        )
        content = response.choices[0].message.content
        parsed = json.loads(content) if isinstance(content, str) else None
        normalized = _normalize_handoff(parsed, allowed_refs, max(1000, output_max_chars))
        handoff = normalized or fallback
    except Exception:
        handoff = fallback
    return json.dumps(handoff, ensure_ascii=False, separators=(",", ":"))
