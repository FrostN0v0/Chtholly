"""Persistence and bounded prompt views for recent tool executions."""

from __future__ import annotations

import json
from typing import cast
from datetime import datetime, timezone
from collections.abc import Sequence

from sqlalchemy import delete, select
from entari_plugin_database import get_session

from .models import Conversation, ToolExecution
from .core.types import JSONType
from .core.tool_trace import ToolTraceEvent
from .core.tool_trace_safety import compact_tool_activity

_MAX_CONTEXT_EVENTS = 32
_MAX_CONTEXT_CHARS = 12000
_MAX_HISTORY_RECORDS = 2000


def _dump_object(value: dict[str, JSONType]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load_object(value: str) -> dict[str, JSONType]:
    try:
        parsed = json.loads(value)
    except ValueError:
        return {}
    return cast(dict[str, JSONType], parsed) if isinstance(parsed, dict) else {}


def _format_timestamp(value: datetime) -> str:
    current = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return current.isoformat(timespec="seconds").replace("+00:00", "Z")


async def persist_tool_events(
    channel_id: str,
    turn_id: int,
    events: Sequence[ToolTraceEvent],
    retention_limit: int,
) -> None:
    """Persist one turn trace and prune old channel records in one transaction."""

    if not events:
        return
    retained = min(_MAX_HISTORY_RECORDS, max(1, int(retention_limit)))
    rows = [
        ToolExecution(
            channel_id=channel_id,
            turn_id=turn_id,
            sequence=event.sequence,
            tool_name=event.tool_name,
            status=event.status,
            effect=event.effect,
            arguments_json=_dump_object(event.arguments),
            outcome_json=_dump_object(event.outcome),
            duration_ms=event.duration_ms,
            started_at=event.started_at,
        )
        for event in sorted(events, key=lambda item: item.sequence)
    ]
    async with get_session() as session:
        session.add_all(rows)
        await session.flush()
        stale_ids = (
            (
                await session.execute(
                    select(ToolExecution.id)
                    .where(ToolExecution.channel_id == channel_id)
                    .order_by(ToolExecution.id.desc())
                    .offset(retained)
                )
            )
            .scalars()
            .all()
        )
        if stale_ids:
            await session.execute(delete(ToolExecution).where(ToolExecution.id.in_(stale_ids)))
        await session.commit()


async def load_recent_tool_activity(
    channel_id: str,
    history: Sequence[Conversation],
    *,
    max_events: int,
    max_chars: int,
) -> list[dict[str, JSONType]]:
    """Load safe events attached to user turns still present in chat context."""

    event_limit = min(_MAX_CONTEXT_EVENTS, max(0, int(max_events)))
    char_limit = min(_MAX_CONTEXT_CHARS, max(0, int(max_chars)))
    if event_limit == 0 or char_limit == 0:
        return []
    user_rows = [row for row in history if row.role == "user" and row.id is not None]
    if not user_rows:
        return []
    turn_ids = [row.id for row in user_rows]
    turn_offsets = {row.id: index - len(user_rows) for index, row in enumerate(user_rows)}
    speakers = {row.id: row.user_name for row in user_rows}
    async with get_session() as session:
        rows = list(
            (
                await session.execute(
                    select(ToolExecution)
                    .where(
                        ToolExecution.channel_id == channel_id,
                        ToolExecution.turn_id.in_(turn_ids),
                    )
                    .order_by(ToolExecution.id.desc())
                    .limit(event_limit)
                )
            )
            .scalars()
            .all()
        )
    rows.reverse()
    activity: list[dict[str, object]] = [
        {
            "turn_offset": turn_offsets.get(row.turn_id, -1),
            "speaker": speakers.get(row.turn_id, ""),
            "tool": row.tool_name,
            "status": row.status,
            "effect": row.effect,
            "arguments": _load_object(row.arguments_json),
            "outcome": _load_object(row.outcome_json),
            "observed_at": _format_timestamp(row.started_at),
            "duration_ms": row.duration_ms,
        }
        for row in rows
    ]
    return compact_tool_activity(activity, max_chars=char_limit)
