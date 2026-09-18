"""Scope-safe query service for persisted context sessions and AgentEvent payloads."""

from __future__ import annotations

import json
from collections.abc import Mapping, Iterator

from sqlalchemy import select
from entari_plugin_database import get_session

from .models import AgentTurn, AgentEvent, ContextSession
from .agent_events import get_event_by_ref
from .agent_context import ContextReadGrant, AgentAccessContext
from .context_reads import payload_digest, public_payload_path, model_readable_payload
from .session_manager import pin_event, get_session_by_ref, list_scope_sessions
from .core.model_audit import ADMIN_ONLY_EVENT_TYPES


class AgentQueryError(ValueError):
    pass


def _require_scope(access: AgentAccessContext | None) -> AgentAccessContext:
    if access is None:
        raise AgentQueryError("Agent history access is not allowed outside llm_chat generation")
    return access


async def _session_scope_id(session_id: int) -> int | None:
    async with get_session() as db:
        context_session = await db.get(ContextSession, session_id)
        return context_session.scope_id if context_session is not None else None


async def _event_session(event_id: int) -> ContextSession | None:
    async with get_session() as db:
        return (
            await db.execute(
                select(ContextSession)
                .join(AgentTurn, AgentTurn.session_id == ContextSession.id)
                .join(AgentEvent, AgentEvent.turn_id == AgentTurn.id)
                .where(AgentEvent.id == event_id)
            )
        ).scalar_one_or_none()


def _authorize_session(
    access: AgentAccessContext,
    context_session: ContextSession,
    *,
    exact_grant: bool = False,
) -> None:
    if context_session.scope_id != access.scope_id:
        raise AgentQueryError("Session is outside llm_chat scope and is not allowed")
    if context_session.status == "sealed":
        raise AgentQueryError("Sealed session access is not allowed")
    if context_session.id != access.session_id and not access.allow_archived_sessions and not exact_grant:
        raise AgentQueryError("Archived session access is not allowed for the current user request")


async def list_sessions_payload(
    access: AgentAccessContext | None,
    *,
    limit: int,
) -> dict[str, object]:
    current = _require_scope(access)
    if not current.allow_archived_sessions:
        raise AgentQueryError("Archived session access is not allowed for the current user request")
    sessions = [
        item for item in await list_scope_sessions(current.scope_id, limit=max(1, limit) * 2) if item.status != "sealed"
    ][: max(1, limit)]
    return {
        "sessions": [
            {
                "session_ref": item.session_ref,
                "sequence": item.sequence,
                "status": item.status,
                "start_reason": item.start_reason,
                "close_reason": item.close_reason,
                "turn_count": item.turn_count,
                "created_at": item.created_at.isoformat(),
                "closed_at": item.closed_at.isoformat() if item.closed_at else None,
            }
            for item in sessions
        ]
    }


async def read_session_handoff_payload(
    access: AgentAccessContext | None,
    session_ref: str,
) -> dict[str, object]:
    current = _require_scope(access)
    context_session = await get_session_by_ref(session_ref.strip())
    if context_session is None:
        raise AgentQueryError("Unknown session_ref")
    _authorize_session(current, context_session)
    try:
        handoff = json.loads(context_session.handoff_json)
    except ValueError:
        handoff = {}
    return {
        "session_ref": context_session.session_ref,
        "status": context_session.status,
        "start_reason": context_session.start_reason,
        "close_reason": context_session.close_reason,
        "handoff": handoff if isinstance(handoff, Mapping) else {},
    }


async def list_tool_executions_payload(
    access: AgentAccessContext | None,
    *,
    session_ref: str = "",
    limit: int = 20,
) -> dict[str, object]:
    current = _require_scope(access)
    session_id = current.session_id
    if session_ref.strip():
        context_session = await get_session_by_ref(session_ref.strip())
        if context_session is None:
            raise AgentQueryError("Unknown session_ref")
        _authorize_session(current, context_session)
        session_id = context_session.id
    async with get_session() as db:
        rows = list(
            (
                await db.execute(
                    select(AgentEvent)
                    .join(AgentTurn, AgentTurn.id == AgentEvent.turn_id)
                    .where(
                        AgentTurn.session_id == session_id,
                        AgentEvent.event_type == "tool_result",
                    )
                    .order_by(AgentEvent.created_at.desc(), AgentEvent.sequence.desc())
                    .limit(max(1, limit))
                )
            )
            .scalars()
            .all()
        )
    return {
        "executions": [
            {
                "event_ref": event.event_ref,
                "execution_ref": event.execution_ref,
                "tool": event.tool_name,
                "status": event.status,
                "effect": event.effect,
                "duration_ms": event.duration_ms,
                "created_at": event.created_at.isoformat(),
            }
            for event in rows
        ]
    }


def _bounded_payload(value: object, *, max_chars: int, offset: int) -> dict[str, object]:
    if isinstance(value, str):
        start = max(0, offset)
        end = min(len(value), start + max_chars)
        return {
            "data": value[start:end],
            "offset": start,
            "next_offset": end if end < len(value) else None,
            "total_chars": len(value),
        }
    serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(serialized) <= max_chars:
        return {"data": value, "offset": 0, "next_offset": None, "total_chars": len(serialized)}
    return {
        "stored": True,
        "chars": len(serialized),
        "error": "Payload is too large; select a narrower path",
    }


async def _readable_event(
    access: AgentAccessContext,
    event: AgentEvent,
) -> tuple[ContextSession, dict[str, object]]:
    if event.event_type in ADMIN_ONLY_EVENT_TYPES or not event.model_visible:
        raise AgentQueryError("Private audit events are not model-readable")
    context_session = await _event_session(event.id)
    if context_session is None:
        raise AgentQueryError("Event has no context session")
    if context_session.scope_id != access.scope_id or context_session.status == "sealed":
        raise AgentQueryError("Event session is sealed or outside llm_chat scope")
    if context_session.id != access.session_id:
        async with get_session() as db:
            generation_session = await db.get(ContextSession, access.session_id)
            if (
                generation_session is None
                or generation_session.scope_id != access.scope_id
                or generation_session.status == "sealed"
            ):
                raise AgentQueryError("Generation session is sealed or outside llm_chat scope")
    return context_session, model_readable_payload(event)


def _matching_grants(
    access: AgentAccessContext,
    event: AgentEvent,
    context_session: ContextSession,
    payload: Mapping[str, object],
    *,
    path: str,
) -> Iterator[ContextReadGrant]:
    for grant in access.context_read_grants:
        if (
            grant.scope_id != access.scope_id
            or grant.session_id != context_session.id
            or grant.generation_session_id != access.session_id
            or grant.generation_turn_id != access.turn_id
            or grant.event_ref != event.event_ref
            or grant.execution_ref != event.execution_ref
            or (path and path != grant.path and not path.startswith(grant.path + "."))
        ):
            continue
        try:
            if payload_digest(public_payload_path(payload, grant.path)) == grant.sha256:
                yield grant
        except KeyError:
            continue


def _authorized_payload(
    access: AgentAccessContext,
    event: AgentEvent,
    context_session: ContextSession,
    payload: Mapping[str, object],
    *,
    path: str,
    offset: int,
    max_chars: int,
) -> dict[str, object]:
    if path:
        try:
            selected = public_payload_path(payload, path)
        except KeyError as exc:
            raise AgentQueryError("Unknown or private event payload path is not model-readable") from exc
        granted = next(_matching_grants(access, event, context_session, payload, path=path), None) is not None
        _authorize_session(access, context_session, exact_grant=granted)
        if not granted and not access.allow_payload_delivery:
            raise AgentQueryError("Stored payload access is not allowed for the current user request")
        return _bounded_payload(selected, max_chars=max(256, max_chars), offset=offset)
    grants = tuple(_matching_grants(access, event, context_session, payload, path=""))
    _authorize_session(access, context_session, exact_grant=bool(grants))
    compact: dict[str, object]
    if context_session.id != access.session_id and not access.allow_archived_sessions:
        # Exact references never authorize a parent object or sibling field.
        compact = {
            "references": [
                {"event_ref": grant.event_ref, "path": grant.path, "sha256": grant.sha256, "stored": True}
                for grant in grants
            ]
        }
    else:
        compact = model_readable_payload(event, compact=True)
    return _bounded_payload(compact, max_chars=max(256, max_chars), offset=0)


async def read_event_payload(
    access: AgentAccessContext | None,
    *,
    event_ref: str,
    path: str = "",
    offset: int = 0,
    max_chars: int,
) -> dict[str, object]:
    current = _require_scope(access)
    event = await get_event_by_ref(event_ref.strip())
    if event is None:
        raise AgentQueryError("Unknown event_ref")
    context_session, payload = await _readable_event(current, event)
    data = _authorized_payload(
        current,
        event,
        context_session,
        payload,
        path=path,
        offset=offset,
        max_chars=max_chars,
    )
    return {
        "event_ref": event.event_ref,
        "event_type": event.event_type,
        "tool": event.tool_name,
        "status": event.status,
        "effect": event.effect,
        "path": path,
        **data,
    }


async def read_tool_execution_payload(
    access: AgentAccessContext | None,
    *,
    execution_ref: str,
    path: str = "",
    offset: int = 0,
    max_chars: int,
) -> dict[str, object]:
    current = _require_scope(access)
    async with get_session() as db:
        events = list(
            (
                await db.execute(
                    select(AgentEvent)
                    .where(
                        AgentEvent.execution_ref == execution_ref.strip(),
                        AgentEvent.event_type.in_(("assistant_tool_call", "tool_result")),
                        AgentEvent.model_visible.is_(True),
                    )
                    .order_by(AgentEvent.sequence.asc())
                )
            )
            .scalars()
            .all()
        )
    call_event = next((event for event in events if event.event_type == "assistant_tool_call"), None)
    result_event = next((event for event in events if event.event_type == "tool_result"), None)
    if call_event is None or result_event is None:
        raise AgentQueryError("Unknown execution_ref")
    call_session, call_payload = await _readable_event(current, call_event)
    result_session, result_payload = await _readable_event(current, result_event)
    if (
        call_event.turn_id != result_event.turn_id
        or call_event.attempt != result_event.attempt
        or call_session.id != result_session.id
    ):
        raise AgentQueryError("Execution events do not belong to the same turn")
    if path:
        root = path.split(".", 1)[0]
        if root not in {"arguments", "result"}:
            raise AgentQueryError("Unknown or private event payload path")
        target, owner, payload = (
            (call_event, call_session, call_payload)
            if root == "arguments"
            else (result_event, result_session, result_payload)
        )
        data = _authorized_payload(
            current,
            target,
            owner,
            payload,
            path=path,
            offset=offset,
            max_chars=max_chars,
        )
    else:
        granted_events = [
            (event, owner, payload)
            for event, owner, payload in (
                (call_event, call_session, call_payload),
                (result_event, result_session, result_payload),
            )
            if next(_matching_grants(current, event, owner, payload, path=""), None) is not None
        ]
        _authorize_session(current, result_session, exact_grant=bool(granted_events))
        compact: dict[str, object]
        if result_session.id != current.session_id and not current.allow_archived_sessions:
            compact = {
                "references": [
                    {"event_ref": grant.event_ref, "path": grant.path, "sha256": grant.sha256, "stored": True}
                    for event, owner, payload in granted_events
                    for grant in _matching_grants(current, event, owner, payload, path="")
                ]
            }
        else:
            compact = {
                "arguments": model_readable_payload(call_event, compact=True).get("arguments", {}),
                "result": model_readable_payload(result_event, compact=True).get("result", {}),
            }
        data = _bounded_payload(compact, max_chars=max(256, max_chars), offset=0)
    return {
        "execution_ref": result_event.execution_ref,
        "call_event_ref": call_event.event_ref,
        "result_event_ref": result_event.event_ref,
        "tool": result_event.tool_name,
        "status": result_event.status,
        "effect": result_event.effect,
        "path": path,
        **data,
    }


async def pin_context_payload(
    access: AgentAccessContext | None,
    *,
    event_ref: str,
    label: str,
) -> dict[str, object]:
    current = _require_scope(access)
    if not current.allow_context_pin:
        raise AgentQueryError("Context pinning is not allowed for the current user request")
    event = await get_event_by_ref(event_ref.strip())
    if event is None:
        raise AgentQueryError("Unknown event_ref")
    if event.event_type in ADMIN_ONLY_EVENT_TYPES or not event.model_visible:
        raise AgentQueryError("Private audit events cannot be pinned")
    context_session = await _event_session(event.id)
    if context_session is None or context_session.scope_id != current.scope_id:
        raise AgentQueryError("Event is outside llm_chat scope and is not allowed")
    anchor = await pin_event(
        current.scope_id,
        event.id,
        label=label.strip() or "Pinned context",
        created_by_user_id=current.user_id,
    )
    return {"pinned": True, "event_ref": event.event_ref, "label": anchor.label}
