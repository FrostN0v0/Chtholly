"""Authenticated reminder task and source-session read projections."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy import String, Integer, or_, and_, cast, func, select
from entari_plugin_database import get_session

from .models import AgentTurn, ChatScope, AgentEvent, ReminderTask, ContextSession, ReminderOccurrence
from .reminders import store
from .agent_events import load_event_payload
from .agent_event_view import event_preview

_STATUSES = frozenset({"active", "paused", "completed", "cancelled"})
_OCCURRENCE_STATES = frozenset(
    {
        "claimed",
        "model_started",
        "dm_started",
        "transport_started",
        "confirmed",
        "completed",
        "paused",
        "cancelled",
        "expired_recovered",
        "unknown",
    }
)


class _RequestError(Exception):
    def __init__(self, message: str, code: str = "invalid_request", status: int = 400) -> None:
        self.message, self.code, self.status = message, code, status


def _utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _project(task: ReminderTask, occurrence: ReminderOccurrence | None, scope_ref: str | None) -> dict[str, object]:
    # Never serialize ORM entities: lease tokens, receipt IDs and source/audit refs are private.
    return {
        "task_ref": task.task_ref,
        "scope_ref": scope_ref,
        "revision": task.revision,
        "owner_user_id": task.unified_user_id,
        "platform_user_id": task.platform_user_id,
        "goal": task.goal[: store.MAX_GOAL],
        "background": task.background[: store.MAX_BACKGROUND],
        "continuation_state": task.continuation_state[: store.MAX_CONTINUATION],
        "mode": task.mode if task.mode in {"once", "adaptive"} else "unknown",
        "origin": task.origin if task.origin in {"user", "autonomous"} else "unknown",
        "delivery": task.delivery_target_type if task.delivery_target_type in {"channel", "dm"} else "unknown",
        "status": task.status if task.status in _STATUSES else "unknown",
        "due_at": _utc(task.due_at),
        "timezone": task.timezone[: store.MAX_TIMEZONE],
        "expires_at": _utc(task.expires_at),
        "created_at": _utc(task.created_at),
        "updated_at": _utc(task.updated_at),
        "paused_reason": task.paused_reason[: store.MAX_REASON],
        "occurrence": None
        if occurrence is None
        else {
            "state": occurrence.state if occurrence.state in _OCCURRENCE_STATES else "unknown",
            "scheduled_at": _utc(occurrence.scheduled_at),
            "started_at": _utc(occurrence.started_at),
            "finished_at": _utc(occurrence.finished_at),
            "confirmed_count": max(0, occurrence.confirmed_count),
            "transport_started": occurrence.transport_started,
        },
    }


async def _scope_id(db, scope_ref: str) -> int | None:
    if scope_ref in ("all", "unassigned"):
        return None
    result = await db.scalar(select(ChatScope.id).where(ChatScope.scope_ref == scope_ref))
    if result is None:
        raise _RequestError("聊天范围不存在", "scope_not_found", 404)
    return result


def _source_rows():
    """Link creation turns only through the task's real account and chat scope."""
    return (
        select(ReminderTask, ChatScope, AgentTurn, ContextSession)
        .outerjoin(
            ChatScope,
            and_(
                ChatScope.id == ReminderTask.source_scope_id,
                ChatScope.platform == ReminderTask.platform,
                ChatScope.account_id == ReminderTask.account_id,
                or_(ReminderTask.source_channel_id == "", ChatScope.channel_id == ReminderTask.source_channel_id),
            ),
        )
        .outerjoin(
            AgentTurn,
            and_(
                ChatScope.id.is_not(None),
                AgentTurn.id == cast(ReminderTask.source_turn_ref, Integer),
                cast(AgentTurn.id, String) == ReminderTask.source_turn_ref,
                AgentTurn.user_id == ReminderTask.unified_user_id,
                AgentTurn.trigger_kind != "scheduled",
                or_(ReminderTask.source_event_ref == "", AgentTurn.trigger_message_id == ReminderTask.source_event_ref),
            ),
        )
        .outerjoin(
            ContextSession,
            and_(
                ContextSession.id == AgentTurn.session_id,
                ContextSession.scope_id == ChatScope.id,
                ContextSession.purpose == "chat",
                ContextSession.reminder_task_ref == "",
            ),
        )
    )


async def _source_session_id(db, scope_ref: str, scope_id: int | None, session_ref: str) -> int | None:
    if session_ref in ("all", "unlinked"):
        return None
    if scope_ref == "unassigned":
        raise _RequestError("来源会话不属于此范围", "session_not_found", 404)
    session = await db.scalar(
        select(ContextSession).where(
            ContextSession.session_ref == session_ref,
            ContextSession.purpose == "chat",
            ContextSession.reminder_task_ref == "",
        )
    )
    if session is None or (scope_id is not None and session.scope_id != scope_id):
        raise _RequestError("来源会话不属于此范围", "session_not_found", 404)
    return session.id


async def _latest_occurrences(db, task_refs: list[str]) -> dict[str, ReminderOccurrence]:
    if not task_refs:
        return {}
    latest = (
        select(ReminderOccurrence.task_ref, func.max(ReminderOccurrence.sequence).label("sequence"))
        .where(ReminderOccurrence.task_ref.in_(task_refs))
        .group_by(ReminderOccurrence.task_ref)
        .subquery()
    )
    rows = await db.execute(
        select(ReminderOccurrence).join(
            latest,
            and_(
                ReminderOccurrence.task_ref == latest.c.task_ref,
                ReminderOccurrence.sequence == latest.c.sequence,
            ),
        )
    )
    return {row.task_ref: row for row in rows.scalars()}


async def _list_projections(
    scope_ref: str, session_ref: str, status: str, q: str, limit: int, offset: int
) -> tuple[list[dict[str, object]], bool, int]:
    if status not in _STATUSES | {"all"} or not session_ref or not scope_ref:
        raise _RequestError("筛选条件无效")
    async with get_session() as db:
        scope_id = await _scope_id(db, scope_ref)
        session_id = await _source_session_id(db, scope_ref, scope_id, session_ref)
        statement = _source_rows()
        if scope_ref == "unassigned":
            statement = statement.where(ChatScope.id.is_(None))
        elif scope_id is not None:
            statement = statement.where(ChatScope.id == scope_id)
        if session_ref == "unlinked":
            statement = statement.where(ContextSession.id.is_(None))
        elif session_id is not None:
            statement = statement.where(ContextSession.id == session_id)
        if status != "all":
            statement = statement.where(ReminderTask.status == status)
        if q.strip():
            literal = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{literal}%"
            statement = statement.where(
                or_(
                    ReminderTask.goal.ilike(pattern, escape="\\"),
                    ReminderTask.unified_user_id.ilike(pattern, escape="\\"),
                )
            )
        total_count = await db.scalar(statement.with_only_columns(func.count(ReminderTask.id)).order_by(None)) or 0
        rows = (
            await db.execute(
                statement.order_by(ReminderTask.updated_at.desc(), ReminderTask.id.desc()).offset(offset).limit(limit)
            )
        ).all()
        occurrences = await _latest_occurrences(db, [task.task_ref for task, _, _, _ in rows])
        items = []
        for task, scope, _turn, session in rows:
            item = _project(task, occurrences.get(task.task_ref), scope.scope_ref if scope is not None else None)
            item["source_session_ref"] = session.session_ref if session is not None else None
            items.append(item)
        return items, offset + len(items) < total_count, total_count


async def _scopes(limit: int, offset: int) -> tuple[list[dict[str, object]], bool]:
    async with get_session() as db:
        counts = (
            select(
                ChatScope.id.label("scope_id"),
                func.count(ReminderTask.id).label("task_count"),
                func.count(ReminderTask.id).filter(ReminderTask.status == "active").label("active_count"),
            )
            .join(
                ReminderTask,
                and_(
                    ReminderTask.source_scope_id == ChatScope.id,
                    ReminderTask.platform == ChatScope.platform,
                    ReminderTask.account_id == ChatScope.account_id,
                    or_(ReminderTask.source_channel_id == "", ReminderTask.source_channel_id == ChatScope.channel_id),
                ),
            )
            .group_by(ChatScope.id)
            .subquery()
        )
        rows = (
            await db.execute(
                select(ChatScope, counts.c.task_count, counts.c.active_count)
                .join(counts, ChatScope.id == counts.c.scope_id)
                .order_by(ChatScope.updated_at.desc(), ChatScope.id.desc())
                .offset(offset)
                .limit(limit + 1)
            )
        ).all()
        return [
            {
                "scope_ref": scope.scope_ref,
                "channel_name": scope.display_name or scope.channel_id,
                "platform": scope.platform,
                "account_id": scope.account_id,
                "channel_id": scope.channel_id,
                "task_count": count or 0,
                "active_count": active or 0,
            }
            for scope, count, active in rows[:limit]
        ], len(rows) > limit


async def _sessions(scope_ref: str, limit: int, offset: int) -> tuple[list[dict[str, object]], bool, int]:
    if scope_ref in ("all", "unassigned", ""):
        raise _RequestError("请选择具体聊天范围")
    async with get_session() as db:
        scope_id = await _scope_id(db, scope_ref)
        query = select(ContextSession).where(
            ContextSession.scope_id == scope_id,
            ContextSession.purpose == "chat",
            ContextSession.reminder_task_ref == "",
        )
        total = await db.scalar(query.with_only_columns(func.count(ContextSession.id)).order_by(None)) or 0
        sessions = (
            (
                await db.execute(
                    query.order_by(ContextSession.sequence.desc(), ContextSession.id.desc()).offset(offset).limit(limit)
                )
            )
            .scalars()
            .all()
        )
        counts: dict[int, int] = {}
        if sessions:
            session_ids = [item.id for item in sessions]
            rows = await db.execute(
                select(AgentTurn.session_id, func.count(ReminderTask.id))
                .join(ContextSession, ContextSession.id == AgentTurn.session_id)
                .join(ChatScope, ChatScope.id == ContextSession.scope_id)
                .join(
                    ReminderTask,
                    and_(
                        ReminderTask.source_scope_id == ChatScope.id,
                        ReminderTask.platform == ChatScope.platform,
                        ReminderTask.account_id == ChatScope.account_id,
                        or_(
                            ReminderTask.source_channel_id == "", ReminderTask.source_channel_id == ChatScope.channel_id
                        ),
                        ReminderTask.unified_user_id == AgentTurn.user_id,
                        AgentTurn.trigger_kind != "scheduled",
                        ReminderTask.source_turn_ref == cast(AgentTurn.id, String),
                        or_(
                            ReminderTask.source_event_ref == "",
                            ReminderTask.source_event_ref == AgentTurn.trigger_message_id,
                        ),
                    ),
                )
                .where(AgentTurn.session_id.in_(session_ids))
                .group_by(AgentTurn.session_id)
            )
            for session_id, count in rows:
                counts[session_id] = count
        return (
            [
                {
                    "session_ref": item.session_ref,
                    "sequence": item.sequence,
                    "status": item.status,
                    "created_at": _utc(item.created_at),
                    "task_count": counts.get(item.id, 0),
                }
                for item in sessions
            ],
            offset + len(sessions) < total,
            total,
        )


def _safe_preview(value: str, maximum: int = 400) -> str:
    """Show bounded chat excerpts without credential literals, URLs or local paths."""
    text = re.sub(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|role[_-]?token|password|secret|cookie|authorization)\b[\"']?\s*[:=]\s*[\"']?[^\s,;\"'}]+",
        "[凭证已隐藏]",
        value,
    )
    text = re.sub(r"(?i)\bsk-[a-z0-9_-]{8,}\b|\bBearer\s+[a-z0-9._-]{8,}\b", "[凭证已隐藏]", text)
    text = re.sub(
        r"(?i)https?://[^\s<>\"']+|\b[A-Za-z]:\\[^\s<>\"']+|/(?:etc|opt|var|home|Users|root|tmp)/[^\s<>\"']+",
        "[地址已隐藏]",
        text,
    )
    return text[: maximum - 1] + "…" if len(text) > maximum else text


async def _source_preview(db, turn_id: int) -> str | None:
    event = await db.scalar(
        select(AgentEvent)
        .where(AgentEvent.turn_id == turn_id, AgentEvent.event_type == "user_input")
        .order_by(AgentEvent.sequence.asc())
        .limit(1)
    )
    return _safe_preview(event_preview(event, load_event_payload(event))) if event is not None else None


async def _task_projection(task_ref: str, *, include_source: bool = False) -> dict[str, object]:
    async with get_session() as db:
        row = (await db.execute(_source_rows().where(ReminderTask.task_ref == task_ref))).one_or_none()
        if row is None:
            raise _RequestError("任务不存在", "task_not_found", 404)
        task, scope, turn, session = row
        occurrences = await _latest_occurrences(db, [task_ref])
        item = _project(task, occurrences.get(task_ref), scope.scope_ref if scope is not None else None)
        item["source_session_ref"] = session.session_ref if session is not None else None
        if include_source:
            item["source"] = (
                None
                if scope is None
                else {
                    "scope_ref": scope.scope_ref,
                    "session_ref": session.session_ref if session is not None else None,
                    "turn_ref": turn.turn_ref if session is not None and turn is not None else None,
                    "input_preview": await _source_preview(db, turn.id)
                    if session is not None and turn is not None
                    else None,
                }
            )
        return item
