"""Agent turn event recording, persistence, and bounded payload access."""

from __future__ import annotations

import json
import asyncio
from collections.abc import Mapping, Sequence

from sqlalchemy import select, update
from entari_plugin_database import get_session

from .models import AgentTurn, AgentEvent, ContextSession
from .core.types import JSONType
from .core.agent_trace import AgentEventDraft


async def persist_agent_events(turn_id: int, events: Sequence[AgentEventDraft]) -> None:
    if not events:
        return
    rows = [
        AgentEvent(
            turn_id=turn_id,
            sequence=event.sequence,
            attempt=event.attempt,
            event_type=event.event_type,
            role=event.role,
            tool_call_id=event.tool_call_id,
            execution_ref=event.execution_ref,
            tool_name=event.tool_name,
            payload_json=json.dumps(event.payload, ensure_ascii=False, separators=(",", ":")),
            status=event.status,
            effect=event.effect,
            duration_ms=event.duration_ms,
            model_visible=event.model_visible,
            created_at=event.created_at,
        )
        for event in sorted(events, key=lambda item: item.sequence)
    ]
    async with get_session() as db:
        db.add_all(rows)
        await db.commit()


async def settle_background_tool_result(
    turn_id: int,
    execution_ref: str,
    *,
    status: str,
    effect: str,
    result: Mapping[str, object],
    duration_ms: int,
    wait_seconds: float = 30.0,
) -> bool:
    """Replace one pending tool result after its detached side effect settles."""

    payload = json.dumps(
        {"result": dict(result), "context_result": dict(result)},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    attempts = max(1, round(max(0.0, wait_seconds) / 0.25))
    for attempt in range(attempts):
        async with get_session() as db:
            updated = await db.execute(
                update(AgentEvent)
                .where(
                    AgentEvent.turn_id == turn_id,
                    AgentEvent.execution_ref == execution_ref,
                    AgentEvent.event_type == "tool_result",
                    AgentEvent.tool_name == "tag_image",
                )
                .values(
                    payload_json=payload,
                    status=status,
                    effect=effect,
                    duration_ms=max(0, int(duration_ms)),
                )
            )
            await db.commit()
        if getattr(updated, "rowcount", None):
            return True
        if attempt + 1 < attempts:
            await asyncio.sleep(0.25)
    return False


def load_event_payload(event: AgentEvent) -> dict[str, JSONType]:
    try:
        payload = json.loads(event.payload_json)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


async def load_turn_events(turn_id: int, *, model_visible_only: bool = False) -> list[AgentEvent]:
    clauses = [AgentEvent.turn_id == turn_id]
    if model_visible_only:
        clauses.append(AgentEvent.model_visible.is_(True))
    async with get_session() as db:
        return list(
            (await db.execute(select(AgentEvent).where(*clauses).order_by(AgentEvent.sequence.asc()))).scalars().all()
        )


async def load_session_turns(session_id: int, *, limit: int = 100) -> list[AgentTurn]:
    async with get_session() as db:
        rows = list(
            (
                await db.execute(
                    select(AgentTurn)
                    .where(
                        AgentTurn.session_id == session_id,
                        AgentTurn.status.in_(("completed", "partial")),
                    )
                    .order_by(AgentTurn.sequence.desc())
                    .limit(max(1, limit))
                )
            )
            .scalars()
            .all()
        )
    rows.reverse()
    return rows


async def load_session_events(
    session_id: int,
    *,
    model_visible_only: bool = False,
    turn_limit: int = 100,
) -> list[tuple[AgentTurn, list[AgentEvent]]]:
    turns = await load_session_turns(session_id, limit=turn_limit)
    return [(turn, await load_turn_events(turn.id, model_visible_only=model_visible_only)) for turn in turns]


async def get_event_by_ref(event_ref: str) -> AgentEvent | None:
    async with get_session() as db:
        return (await db.execute(select(AgentEvent).where(AgentEvent.event_ref == event_ref))).scalar_one_or_none()


async def event_scope_id(event: AgentEvent) -> int | None:
    async with get_session() as db:
        turn = await db.get(AgentTurn, event.turn_id)
        if turn is None:
            return None
        context_session = await db.get(ContextSession, turn.session_id)
        return context_session.scope_id if context_session is not None else None


def select_payload_path(payload: object, path: str) -> object:
    current = payload
    if not path:
        return current
    for part in path.split("."):
        if isinstance(current, Mapping) and part in current:
            current = current[part]
            continue
        if isinstance(current, Sequence) and not isinstance(current, (str, bytes, bytearray)) and part.isdigit():
            index = int(part)
            if 0 <= index < len(current):
                current = current[index]
                continue
        raise KeyError(path)
    return current
