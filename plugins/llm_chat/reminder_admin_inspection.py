"""Read-only historical execution projections for reminder administration."""

from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import func, select
from entari_plugin_database import get_session

from .models import AgentTurn, ChatScope, AgentEvent, ReminderTask, ContextSession, ReminderOccurrence
from .agent_events import load_event_payload
from .session_inspection import (
    project_context,
    project_outputs,
    summarize_usage,
    project_tool_calls,
    project_model_calls,
)
from .reminder_admin_queries import _STATUSES, _OCCURRENCE_STATES, _utc, _RequestError, _safe_preview


def _audit_time(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 64 or not value:
        return None
    try:
        return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _occurrence_projection(item: ReminderOccurrence, turn_ref: str | None) -> dict[str, object]:
    return {
        "sequence": item.sequence,
        "state": item.state if item.state in _OCCURRENCE_STATES else "unknown",
        "scheduled_at": _utc(item.scheduled_at),
        "started_at": _utc(item.started_at),
        "model_started_at": _utc(item.model_started_at),
        "dm_started_at": _utc(item.dm_started_at),
        "transport_started_at": _utc(item.transport_started_at),
        "finished_at": _utc(item.finished_at),
        "model_started": bool(item.model_started),
        "dm_started": bool(item.dm_started),
        "transport_started": bool(item.transport_started),
        "confirmed_count": max(0, item.confirmed_count),
        "failure_reason": item.failure_reason if re.fullmatch(r"[a-z_]{1,64}", item.failure_reason or "") else None,
        "turn_ref": turn_ref,
    }


async def _occurrence_turn(db, task: ReminderTask, item: ReminderOccurrence):
    if not item.turn_ref:
        return None
    statement = (
        select(AgentTurn, ContextSession, ChatScope)
        .join(ContextSession, AgentTurn.session_id == ContextSession.id)
        .join(ChatScope, ChatScope.id == ContextSession.scope_id)
        .where(
            AgentTurn.turn_ref == item.turn_ref,
            AgentTurn.trigger_kind == "scheduled",
            AgentTurn.user_id == task.unified_user_id,
            ContextSession.purpose == "reminder",
            ContextSession.reminder_task_ref == task.task_ref,
            ChatScope.platform == task.platform,
            ChatScope.account_id == task.account_id,
        )
    )
    if task.delivery_target_type == "channel":
        statement = statement.where(ChatScope.channel_id == task.delivery_channel_id)
    elif task.delivery_target_type == "dm":
        if not task.platform_user_id or task.delivery_user_id != task.platform_user_id:
            return None
    else:
        return None
    return (await db.execute(statement)).one_or_none()


async def _occurrences(task_ref: str, limit: int, offset: int) -> tuple[list[dict[str, object]], bool, int]:
    async with get_session() as db:
        task = await db.scalar(select(ReminderTask).where(ReminderTask.task_ref == task_ref))
        if task is None:
            raise _RequestError("任务不存在", "task_not_found", 404)
        statement = select(ReminderOccurrence).where(ReminderOccurrence.task_ref == task_ref)
        total = (
            await db.scalar(select(func.count(ReminderOccurrence.id)).where(ReminderOccurrence.task_ref == task_ref))
            or 0
        )
        rows = (
            (await db.execute(statement.order_by(ReminderOccurrence.sequence.desc()).offset(offset).limit(limit)))
            .scalars()
            .all()
        )
        items = []
        for row in rows:
            linked = await _occurrence_turn(db, task, row)
            items.append(_occurrence_projection(row, linked[0].turn_ref if linked is not None else None))
        return items, offset + len(items) < total, total


def _recorded_trigger(events: list[AgentEvent], sequence: int) -> dict[str, object] | None:
    for event in events:
        if event.event_type != "reminder_trigger":
            continue
        facts = load_event_payload(event)
        if type(facts.get("occurrence_sequence")) is not int or facts["occurrence_sequence"] != sequence:
            continue
        late = facts.get("late_by_seconds")
        return {
            "scheduled_at": _audit_time(facts.get("scheduled_at")),
            "now": _audit_time(facts.get("now")),
            "late_by_seconds": late if type(late) is int and 0 <= late <= 315360000 else None,
            "mode": facts.get("mode") if facts.get("mode") in ("once", "adaptive") else "unknown",
            "event_at": _utc(event.created_at),
        }
    return None


def _recorded_decision(events: list[AgentEvent], task_ref: str) -> dict[str, object] | None:
    for event in reversed(events):
        if event.event_type != "reminder_decision":
            continue
        payload = load_event_payload(event)
        if payload.get("task_ref") != task_ref:
            continue
        reason = payload.get("reason")
        return {
            "status": payload["status"]
            if isinstance(payload.get("status"), str) and payload["status"] in _STATUSES
            else "unknown",
            "next_due_at": _audit_time(payload.get("next_due_at")),
            "reason": reason if isinstance(reason, str) and re.fullmatch(r"[a-z_]{1,64}", reason) else None,
            "revision": payload.get("revision") if type(payload.get("revision")) is int else None,
            "event_at": _utc(event.created_at),
        }
    return None


def _bounded_refs(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [ref for ref in value[:100] if isinstance(ref, str) and 0 < len(ref) <= 128]


def _inspection(turn: AgentTurn | None, events: list[AgentEvent]) -> dict[str, object]:
    context = project_context(events)
    raw_selection = context.get("selection")
    selection = raw_selection if isinstance(raw_selection, dict) else {}
    raw_budgets = context.get("budgets")
    budgets = raw_budgets if isinstance(raw_budgets, dict) else {}
    raw_source = selection.get("ordinary_chat")
    source = raw_source if isinstance(raw_source, dict) else None
    event_ref = context.get("event_ref")
    raw_blocks = context.get("blocks")
    blocks = raw_blocks if isinstance(raw_blocks, list) else []
    models = project_model_calls(events, turn_status=turn.status if turn is not None else "not_recorded")
    tools = project_tool_calls(events, turn_status=turn.status if turn is not None else "not_recorded")
    outputs = project_outputs(events)
    return {
        "context": {
            "captured": context["captured"],
            "capture_status": context["capture_status"]
            if context["capture_status"] in ("complete", "redacted", "overflow", "recorded", "not_recorded", "partial")
            else "unknown",
            "event_ref": event_ref
            if isinstance(event_ref, str) and re.fullmatch(r"event_[0-9a-f]{20}", event_ref)
            else None,
            "estimated_tokens": selection.get("estimated_tokens")
            if type(selection.get("estimated_tokens")) is int
            else None,
            "selection": {
                "included_turn_refs": _bounded_refs(selection.get("included_turn_refs")),
                "excluded_turn_refs": _bounded_refs(selection.get("excluded_turn_refs")),
                "ordinary_chat": {
                    "status": source.get("status")
                    if source.get("status") in ("present", "empty", "absent")
                    else "absent",
                    "session_ref": source.get("session_ref")
                    if isinstance(source.get("session_ref"), str) and len(source["session_ref"]) <= 128
                    else None,
                    "included_turn_refs": _bounded_refs(source.get("included_turn_refs")),
                }
                if source is not None
                else None,
            },
            "budgets": {
                key: value
                for key, value in budgets.items()
                if key in ("max_input_tokens", "output_reserve_tokens", "minimum_recent_turns", "inline_event_chars")
                and type(value) is int
                and 0 <= value <= 1_000_000
            },
            "blocks": [
                {"name": str(block.get("name", ""))[:80], "chars": block.get("chars"), "path": block.get("path")}
                for block in blocks[:16]
                if isinstance(block, dict) and isinstance(block.get("name"), str)
            ],
        },
        "model_calls": [
            {
                "model": _safe_preview(str(call["model"]), 120),
                "attempt": call["attempt"],
                "status": call["status"],
                "duration_ms": call["duration_ms"],
                "usage": call["usage"],
                "request_event_ref": call["request_event_ref"],
                "response_event_ref": call["response_event_ref"],
                "capture_status": call["capture_status"]
                if call["capture_status"] in ("recorded", "not_recorded", "partial")
                else "unknown",
            }
            for call in models
        ],
        "tool_calls": [
            {
                "tool_name": _safe_preview(str(call["tool_name"]), 100),
                "attempt": call["attempt"],
                "status": call["status"],
                "effect": call["effect"],
                "duration_ms": call["duration_ms"],
                "call_event_ref": call["call_event_ref"],
                "result_event_ref": call["result_event_ref"],
                "arguments_capture_status": call["arguments_capture_status"]
                if call["arguments_capture_status"] in ("recorded", "not_recorded", "partial")
                else "unknown",
                "result_capture_status": call["result_capture_status"]
                if call["result_capture_status"] in ("recorded", "not_recorded", "partial")
                else "unknown",
            }
            for call in tools
        ],
        "outputs": [
            {
                "event_type": output["event_type"],
                "source": output["source"],
                "status": output["status"],
                "confirmed_at": _audit_time(output["confirmed_at"]),
                "preview": _safe_preview(str(output["preview"]), 800),
                "content": _safe_preview(str(output["content"]), 1600),
            }
            for output in outputs
        ],
        "usage": summarize_usage(events),
        "turn": None
        if turn is None
        else {
            "turn_ref": turn.turn_ref,
            "sequence": turn.sequence,
            "status": turn.status,
            "trigger_kind": turn.trigger_kind,
            "created_at": _utc(turn.created_at),
            "finished_at": _utc(turn.finished_at),
        },
    }


def _recorded_environment(events: list[AgentEvent], session: ContextSession, scope: ChatScope) -> dict[str, object]:
    request = next((event for event in events if event.event_type == "model_request"), None)
    recorded_model = load_event_payload(request).get("model") if request is not None else None
    snapshot = next((event for event in events if event.event_type == "context_snapshot"), None)
    recorded_persona = load_event_payload(snapshot).get("persona") if snapshot is not None else None
    persona = None
    if isinstance(recorded_persona, dict):
        persona = {
            key: _safe_preview(value, 120)
            for key in ("key", "name")
            if isinstance(value := recorded_persona.get(key), str)
        } or None
    return {
        "session_ref": session.session_ref,
        "scope_ref": scope.scope_ref,
        "delivery_channel_id": scope.channel_id,
        "platform": scope.platform,
        "account_id": scope.account_id,
        "model": _safe_preview(recorded_model, 120) if isinstance(recorded_model, str) else None,
        "baseline_model": session.model_name,
        "persona": persona,
        "persona_hash": session.persona_hash,
        "system_version": session.system_version,
        "tool_schema_hash": session.tool_schema_hash,
        "policy_version": session.policy_version,
        "created_at": _utc(session.created_at),
    }


async def _occurrence_detail(task_ref: str, sequence: int) -> dict[str, object]:
    if sequence < 1:
        raise _RequestError("发生序号无效")
    async with get_session() as db:
        task = await db.scalar(select(ReminderTask).where(ReminderTask.task_ref == task_ref))
        if task is None:
            raise _RequestError("任务不存在", "task_not_found", 404)
        item = await db.scalar(
            select(ReminderOccurrence).where(
                ReminderOccurrence.task_ref == task_ref, ReminderOccurrence.sequence == sequence
            )
        )
        if item is None:
            raise _RequestError("执行记录不存在", "occurrence_not_found", 404)
        linked = await _occurrence_turn(db, task, item)
        events = []
        if linked is not None:
            events = list(
                (
                    await db.execute(
                        select(AgentEvent).where(AgentEvent.turn_id == linked[0].id).order_by(AgentEvent.sequence.asc())
                    )
                ).scalars()
            )
        turn, session, scope = linked if linked is not None else (None, None, None)
        return {
            **_occurrence_projection(item, turn.turn_ref if turn is not None else None),
            "trigger": _recorded_trigger(events, sequence),
            "decision": _recorded_decision(events, task_ref),
            "inspection": _inspection(turn, events),
            "environment": _recorded_environment(events, session, scope)
            if session is not None and scope is not None
            else None,
        }
