"""Token-budget context selection over persisted agent session events."""

from __future__ import annotations

import json
from hashlib import sha256
from dataclasses import dataclass
from collections.abc import Mapping, Sequence

import litellm

from .models import AgentTurn, AgentEvent, ContextAnchor, ContextSession
from .core.types import ChatMessage
from .agent_events import load_event_payload, load_session_events
from .session_manager import BaselineFingerprint

SYSTEM_SCAFFOLD_VERSION = "agent-context-v1"
AGENT_POLICY_VERSION = "agent-events-v1"
_ARCHIVED_CONTEXT_TERMS = (
    "上次",
    "之前",
    "以前",
    "旧会话",
    "历史会话",
    "上一轮会话",
)
_FRESH_CONTEXT_TERMS = (
    "别管之前",
    "忽略前文",
    "不要参考前文",
    "重新开始这个问题",
    "当成新问题",
)
_PAYLOAD_REQUEST_TERMS = (
    "源码",
    "源代码",
    "工具返回",
    "调用结果",
    "完整结果",
    "继续发",
    "那个页面",
    "那个结果",
    "刚才那个",
    "前面那个",
    "继续修改",
    "继续调整",
)
_PIN_CONTEXT_TERMS = (
    "记住这个上下文",
    "后面都按这个",
    "固定这个",
    "保留这个上下文",
)


@dataclass(frozen=True, slots=True)
class ContextSelection:
    messages: list[ChatMessage]
    estimated_tokens: int
    full_session_tokens: int
    rollover_required: bool
    included_turn_refs: tuple[str, ...]
    excluded_turn_refs: tuple[str, ...]


def build_baseline_fingerprint(
    *,
    model_name: str,
    persona: str,
    tool_schemas: Sequence[Mapping[str, object]],
) -> BaselineFingerprint:
    tool_payload = json.dumps(list(tool_schemas), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return BaselineFingerprint(
        model_name=model_name,
        persona_hash=sha256(persona.encode("utf-8")).hexdigest(),
        system_version=SYSTEM_SCAFFOLD_VERSION,
        tool_schema_hash=sha256(tool_payload.encode("utf-8")).hexdigest(),
        policy_version=AGENT_POLICY_VERSION,
    )


def requests_archived_context(text: str) -> bool:
    normalized = "".join(text.split()).casefold()
    return any(term in normalized for term in _ARCHIVED_CONTEXT_TERMS)


def requests_fresh_context(text: str) -> bool:
    normalized = "".join(text.split()).casefold()
    return any(term in normalized for term in _FRESH_CONTEXT_TERMS)


def requests_tool_payload(text: str) -> bool:
    normalized = "".join(text.split()).casefold()
    return any(term in normalized for term in _PAYLOAD_REQUEST_TERMS)


def requests_context_pin(text: str) -> bool:
    normalized = "".join(text.split()).casefold()
    return any(term in normalized for term in _PIN_CONTEXT_TERMS)


def estimate_tokens(model_name: str | None, messages: Sequence[Mapping[str, object]]) -> int:
    try:
        count = litellm.token_counter(model=model_name or "", messages=list(messages))
    except Exception:
        serialized = json.dumps(list(messages), ensure_ascii=False, separators=(",", ":"))
        return max(1, (len(serialized) + 2) // 3)
    return max(1, int(count))


def _compact_value(value: object, *, event_ref: str, path: str, inline_chars: int) -> object:
    serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(serialized) <= inline_chars:
        return value
    return {
        "stored": True,
        "event_ref": event_ref,
        "path": path,
        "chars": len(serialized),
        "sha256": sha256(serialized.encode("utf-8")).hexdigest(),
    }


def _compact_payload(value: object, *, event_ref: str, path: str, inline_chars: int) -> object:
    if not isinstance(value, Mapping):
        return _compact_value(value, event_ref=event_ref, path=path, inline_chars=inline_chars)
    return {
        str(key): _compact_value(
            item,
            event_ref=event_ref,
            path=f"{path}.{key}",
            inline_chars=inline_chars,
        )
        for key, item in value.items()
    }


def _turn_messages(
    turn: AgentTurn,
    events: Sequence[AgentEvent],
    *,
    inline_chars: int,
) -> list[ChatMessage]:
    messages: list[ChatMessage] = []
    pending_tool_calls: set[str] = set()
    for event in events:
        payload = load_event_payload(event)
        if event.event_type == "user_input":
            content = payload.get("content")
            if isinstance(content, str) and content:
                messages.append({"role": "user", "content": content})
            continue
        if event.event_type == "assistant_tool_call":
            arguments = _compact_payload(
                payload.get("arguments", {}),
                event_ref=event.event_ref,
                path="arguments",
                inline_chars=inline_chars,
            )
            tool_call_id = event.tool_call_id or event.execution_ref or event.event_ref
            pending_tool_calls.add(tool_call_id)
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": tool_call_id,
                            "type": "function",
                            "function": {
                                "name": event.tool_name,
                                "arguments": json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
                            },
                        }
                    ],
                }
            )
            continue
        if event.event_type == "tool_result":
            tool_call_id = event.tool_call_id or event.execution_ref or event.event_ref
            if tool_call_id not in pending_tool_calls:
                continue
            pending_tool_calls.remove(tool_call_id)
            result = _compact_payload(
                payload.get("result", {}),
                event_ref=event.event_ref,
                path="result",
                inline_chars=inline_chars,
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "name": event.tool_name,
                    "content": json.dumps(
                        {
                            "ok": event.status == "succeeded",
                            "status": event.status,
                            "effect": event.effect,
                            "event_ref": event.event_ref,
                            "data": result,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                }
            )
            continue
        if event.event_type == "assistant_output":
            content = payload.get("content")
            if isinstance(content, str) and content:
                messages.append({"role": "assistant", "content": content})
    return messages


async def select_session_context(
    context_session: ContextSession,
    *,
    system: str,
    current_message: ChatMessage,
    model_name: str | None,
    max_input_tokens: int,
    output_reserve_tokens: int,
    rollover_ratio: float,
    minimum_recent_turns: int,
    inline_event_chars: int,
    fresh_context: bool,
) -> ContextSelection:
    base_messages: list[ChatMessage] = [{"role": "system", "content": system}, current_message]
    base_tokens = estimate_tokens(model_name, base_messages)
    if fresh_context:
        return ContextSelection(
            messages=[current_message],
            estimated_tokens=base_tokens,
            full_session_tokens=base_tokens,
            rollover_required=False,
            included_turn_refs=(),
            excluded_turn_refs=(),
        )

    turn_rows = await load_session_events(context_session.id, model_visible_only=True)
    rendered = [
        (turn, _turn_messages(turn, events, inline_chars=max(256, inline_event_chars))) for turn, events in turn_rows
    ]
    rendered = [(turn, messages) for turn, messages in rendered if messages]
    all_history = [message for _turn, messages in rendered for message in messages]
    full_tokens = estimate_tokens(model_name, [{"role": "system", "content": system}, *all_history, current_message])
    available = max(1024, max_input_tokens - max(0, output_reserve_tokens))
    selected: list[tuple[AgentTurn, list[ChatMessage]]] = []
    excluded: list[AgentTurn] = []
    for turn, messages in reversed(rendered):
        candidate = [item for _turn, items in reversed(selected) for item in items]
        candidate = [*messages, *candidate]
        tokens = estimate_tokens(model_name, [{"role": "system", "content": system}, *candidate, current_message])
        if tokens <= available:
            selected.append((turn, messages))
        else:
            excluded.append(turn)
    selected.reverse()
    history = [message for _turn, messages in selected for message in messages]
    estimated = estimate_tokens(model_name, [{"role": "system", "content": system}, *history, current_message])
    threshold = max(0.1, min(0.95, float(rollover_ratio)))
    rollover_required = bool(excluded) and (
        full_tokens >= int(available * threshold) or len(selected) < max(0, minimum_recent_turns)
    )
    return ContextSelection(
        messages=[*history, current_message],
        estimated_tokens=estimated,
        full_session_tokens=full_tokens,
        rollover_required=rollover_required,
        included_turn_refs=tuple(turn.turn_ref for turn, _messages in selected),
        excluded_turn_refs=tuple(turn.turn_ref for turn in reversed(excluded)),
    )


def parse_handoff(value: str) -> dict[str, object]:
    try:
        payload = json.loads(value)
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def render_session_baseline(
    context_session: ContextSession,
    anchors: Sequence[tuple[ContextAnchor, AgentEvent]],
) -> dict[str, object]:
    return {
        "session_ref": context_session.session_ref,
        "start_reason": context_session.start_reason,
        "handoff": parse_handoff(context_session.handoff_json),
        "anchors": [
            {
                "label": anchor.label,
                "event_ref": event.event_ref,
                "tool": event.tool_name,
                "event_type": event.event_type,
            }
            for anchor, event in anchors
        ],
    }
