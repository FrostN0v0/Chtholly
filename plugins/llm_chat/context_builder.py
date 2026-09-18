"""Token-budget context selection over persisted agent session events."""

from __future__ import annotations

import json
from hashlib import sha256
from dataclasses import dataclass
from collections.abc import Mapping, Sequence

import litellm
from sqlalchemy import select
from entari_plugin_database import get_session

from .models import AgentTurn, AgentEvent, ContextAnchor, ContextSession
from .core.types import ChatMessage
from .agent_events import load_event_payload, load_session_events
from .agent_context import ContextReadGrant, ContextReadReference
from .context_reads import payload_digest, public_payload_path, model_readable_payload
from .session_manager import BaselineFingerprint
from .core.model_audit import ADMIN_ONLY_EVENT_TYPES

SYSTEM_SCAFFOLD_VERSION = "agent-context-v4-affect"
AGENT_POLICY_VERSION = "agent-events-v3-context-grants"
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
    read_references: tuple[ContextReadReference, ...] = ()


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


def _compact_value(
    value: object,
    *,
    event_ref: str,
    path: str,
    inline_chars: int,
    references: list[ContextReadReference] | None = None,
) -> object:
    serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(serialized) <= inline_chars:
        return value
    digest = sha256(serialized.encode("utf-8")).hexdigest()
    if references is not None:
        references.append(ContextReadReference(event_ref, path, digest))
    return {
        "stored": True,
        "event_ref": event_ref,
        "path": path,
        "chars": len(serialized),
        "sha256": digest,
    }


def _compact_payload(
    value: object,
    *,
    event_ref: str,
    path: str,
    inline_chars: int,
    references: list[ContextReadReference] | None = None,
) -> object:
    if not isinstance(value, Mapping):
        return _compact_value(value, event_ref=event_ref, path=path, inline_chars=inline_chars, references=references)
    return {
        str(key): _compact_value(
            item,
            event_ref=event_ref,
            path=f"{path}.{key}",
            inline_chars=inline_chars,
            references=references,
        )
        for key, item in value.items()
    }


def _event_tool_call_id(event: AgentEvent) -> str:
    return event.tool_call_id or event.execution_ref or event.event_ref


def _tool_call_item(
    event: AgentEvent,
    *,
    inline_chars: int,
    references: list[ContextReadReference] | None = None,
) -> dict[str, object]:
    payload = model_readable_payload(event)
    arguments = _compact_payload(
        payload.get("arguments", {}),
        event_ref=event.event_ref,
        path="arguments",
        inline_chars=inline_chars,
        references=references,
    )
    return {
        "id": _event_tool_call_id(event),
        "type": "function",
        "function": {
            "name": event.tool_name,
            "arguments": json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
        },
    }


def _tool_result_message(
    event: AgentEvent,
    *,
    inline_chars: int,
    references: list[ContextReadReference] | None = None,
) -> ChatMessage:
    payload = model_readable_payload(event)
    result = _compact_payload(
        payload.get("result", {}),
        event_ref=event.event_ref,
        path="result",
        inline_chars=inline_chars,
        references=references,
    )
    return {
        "role": "tool",
        "tool_call_id": _event_tool_call_id(event),
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


def _turn_messages(
    _turn: AgentTurn,
    events: Sequence[AgentEvent],
    *,
    inline_chars: int,
    references: list[ContextReadReference] | None = None,
) -> list[ChatMessage]:
    visible_types = {"user_input", "assistant_tool_call", "tool_result", "assistant_output"}
    events = [
        event
        for event in events
        if event.model_visible and event.event_type in visible_types and event.event_type not in ADMIN_ONLY_EVENT_TYPES
    ]
    result_index = {
        (event.attempt, event.execution_ref or _event_tool_call_id(event)): (index, event)
        for index, event in enumerate(events)
        if event.event_type == "tool_result"
    }
    emitted: set[tuple[int, str]] = set()
    messages: list[ChatMessage] = []
    event_index = 0
    while event_index < len(events):
        event = events[event_index]
        if event.event_type == "user_input":
            content = load_event_payload(event).get("content")
            if isinstance(content, str) and content:
                messages.append({"role": "user", "content": content})
            event_index += 1
            continue
        if event.event_type == "assistant_tool_call":
            attempt = event.attempt
            call_events: list[tuple[int, AgentEvent]] = []
            while (
                event_index < len(events)
                and events[event_index].event_type == "assistant_tool_call"
                and events[event_index].attempt == attempt
            ):
                call_events.append((event_index, events[event_index]))
                event_index += 1

            result_events: list[tuple[int, AgentEvent]] = []
            tool_calls: list[dict[str, object]] = []
            for call_index, call_event in call_events:
                key = (call_event.attempt, call_event.execution_ref or _event_tool_call_id(call_event))
                result = result_index.get(key)
                if key in emitted or result is None or result[0] <= call_index:
                    continue
                emitted.add(key)
                tool_calls.append(_tool_call_item(call_event, inline_chars=inline_chars, references=references))
                result_events.append(result)
            if not tool_calls:
                continue

            messages.append({"role": "assistant", "content": None, "tool_calls": tool_calls})
            for _index, result_event in sorted(result_events, key=lambda item: item[0]):
                messages.append(_tool_result_message(result_event, inline_chars=inline_chars, references=references))
            continue
        if event.event_type == "assistant_output":
            content = load_event_payload(event).get("content")
            if isinstance(content, str) and content:
                messages.append({"role": "assistant", "content": content})
        event_index += 1
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
    turn_references: dict[int, list[ContextReadReference]] = {}
    rendered = [
        (
            turn,
            _turn_messages(
                turn,
                events,
                inline_chars=max(256, inline_event_chars),
                references=turn_references.setdefault(turn.id, []),
            ),
        )
        for turn, events in turn_rows
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
        read_references=tuple(ref for turn, _messages in selected for ref in turn_references[turn.id]),
    )


async def collect_context_read_grants(
    context_session: ContextSession,
    selection: ContextSelection,
    anchors: Sequence[tuple[ContextAnchor, AgentEvent]],
    *,
    turn_id: int,
) -> tuple[ContextReadGrant, ...]:
    """Bind only host-selected descriptors and authoritative baseline references.

    No message text, tool-result dictionaries, or model-supplied paths are scanned
    for references. Handoff authority stops at its exact validated event set.
    """
    async with get_session() as db:
        current = await db.get(ContextSession, context_session.id)
        turn = await db.get(AgentTurn, turn_id)
        if (
            current is None
            or current.status == "sealed"
            or current.scope_id != context_session.scope_id
            or turn is None
            or turn.session_id != current.id
        ):
            return ()
        anchor_ids = [anchor.id for anchor, _event in anchors]
        active_anchor_events = (
            set(
                (
                    await db.scalars(
                        select(ContextAnchor.event_id).where(
                            ContextAnchor.id.in_(anchor_ids),
                            ContextAnchor.scope_id == current.scope_id,
                            ContextAnchor.active.is_(True),
                        )
                    )
                ).all()
            )
            if anchor_ids
            else set()
        )
        handoff = parse_handoff(current.handoff_json)
        raw_refs = handoff.get("relevant_event_refs", [])
        handoff_refs = {ref for ref in raw_refs if isinstance(ref, str)} if isinstance(raw_refs, list) else set()
        selected_refs: dict[str, list[ContextReadReference]] = {}
        for reference in selection.read_references:
            selected_refs.setdefault(reference.event_ref, []).append(reference)
        refs = set(selected_refs) | handoff_refs | {event.event_ref for _anchor, event in anchors}
        if not refs:
            return ()
        rows = (
            await db.execute(
                select(AgentEvent, AgentTurn, ContextSession)
                .join(AgentTurn, AgentTurn.id == AgentEvent.turn_id)
                .join(ContextSession, ContextSession.id == AgentTurn.session_id)
                .where(AgentEvent.event_ref.in_(refs))
            )
        ).all()
        grants: dict[tuple[str, str], ContextReadGrant] = {}
        for event, source_turn, source_session in rows:
            if (
                source_session.scope_id != current.scope_id
                or source_session.status == "sealed"
                or not event.model_visible
                or event.event_type in ADMIN_ONLY_EVENT_TYPES
            ):
                continue
            payload = model_readable_payload(event)
            references = []
            if source_session.id == current.id and source_turn.turn_ref in selection.included_turn_refs:
                references.extend(selected_refs.get(event.event_ref, ()))
            if event.id in active_anchor_events or (
                event.event_ref in handoff_refs and source_session.id == current.previous_session_id
            ):
                references.extend(
                    ContextReadReference(event.event_ref, path, payload_digest(value))
                    for path, value in payload.items()
                )
            for reference in references:
                try:
                    digest = payload_digest(public_payload_path(payload, reference.path))
                except KeyError:
                    continue
                if digest != reference.sha256:
                    continue
                grants[(event.event_ref, reference.path)] = ContextReadGrant(
                    scope_id=current.scope_id,
                    session_id=source_session.id,
                    generation_session_id=current.id,
                    generation_turn_id=turn_id,
                    event_ref=event.event_ref,
                    execution_ref=event.execution_ref,
                    path=reference.path,
                    sha256=digest,
                )
        return tuple(grants.values())


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
