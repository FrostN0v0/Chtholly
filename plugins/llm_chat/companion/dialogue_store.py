"""Short native transactions for durable dialogue alarms; never stores live Sessions."""

from __future__ import annotations

from typing import Any
from datetime import datetime
from dataclasses import dataclass
from collections.abc import Callable

from sqlalchemy import select
from entari_plugin_database import Base

from utils.companion_core.dialogue import DialogueDecision, decision_deadlines

from .types import ModeSnapshot
from .models import CompanionMode
from ..models import ChatScope, ScopePersonaSelection
from .persistence import aware, naive, token, session, transaction, mode_snapshot
from .dialogue_models import OWNED_TABLES, CompanionDialogue
from ..session_manager import ScopeIdentity


@dataclass(frozen=True, slots=True)
class DialogueSchedule:
    scope: ChatScope
    mode: ModeSnapshot
    revision: int
    action: str
    not_before: datetime | None
    wake_at: datetime | None
    status: str
    run_token: str | None


def _snapshot(scope: ChatScope, mode: CompanionMode, row: CompanionDialogue) -> DialogueSchedule:
    return DialogueSchedule(
        scope,
        mode_snapshot(mode, scope.id),
        row.revision,
        row.action,
        aware(row.not_before),
        aware(row.wake_at),
        row.status,
        row.run_token,
    )


async def _enabled(db: Any, scope_id: int) -> tuple[ChatScope, CompanionMode] | None:
    scope = await db.get(ChatScope, scope_id)
    mode = await db.get(CompanionMode, scope_id)
    selection = await db.get(ScopePersonaSelection, scope_id)
    if (
        scope is None
        or mode is None
        or mode.mode != "companion"
        or selection is None
        or selection.persona_key != mode.persona_key
    ):
        return None
    return scope, mode


def _matches(row: CompanionDialogue, mode: ModeSnapshot) -> bool:
    return row.mode_revision == mode.revision and row.persona_key == mode.persona_key


async def resolve_scope(
    identity: ScopeIdentity,
    *,
    session_factory: Any = None,
) -> tuple[ChatScope, ModeSnapshot] | None:
    """Read only: an ordinary message must not create or mutate a normal scope."""
    async with session(session_factory) as db:
        scope_id = await db.scalar(
            select(ChatScope.id).where(
                ChatScope.platform == identity.platform,
                ChatScope.account_id == identity.account_id,
                ChatScope.channel_id == identity.channel_id,
            )
        )
        enabled = await _enabled(db, scope_id) if scope_id is not None else None
        return (enabled[0], mode_snapshot(enabled[1], scope_id)) if enabled else None


async def resolve_id(scope_id: int, *, session_factory: Any = None) -> tuple[ChatScope, ModeSnapshot] | None:
    async with session(session_factory) as db:
        enabled = await _enabled(db, scope_id)
        return (enabled[0], mode_snapshot(enabled[1], scope_id)) if enabled else None


async def valid(mode: ModeSnapshot, *, session_factory: Any = None) -> bool:
    resolved = await resolve_id(mode.scope_id, session_factory=session_factory)
    return resolved is not None and (
        resolved[1].revision == mode.revision and resolved[1].persona_key == mode.persona_key
    )


async def initialize_store(*, now: datetime, session_factory: Any = None) -> tuple[DialogueSchedule, ...]:
    moment = naive(now)
    async with transaction(session_factory) as db:
        connection = await db.connection()
        await connection.run_sync(
            lambda conn: Base.metadata.create_all(conn, tables=list(OWNED_TABLES), checkfirst=True)
        )
        rows = (await db.scalars(select(CompanionDialogue))).all()
        restored = []
        for row in rows:
            enabled = await _enabled(db, row.scope_id)
            if row.status == "running":
                # A process died after claiming work. It may already have sent; never replay it.
                row.status, row.action, row.run_token = "unknown", "end", None
                row.not_before = row.wake_at = None
                row.revision += 1
                row.updated_at = moment
            if enabled is None or not _matches(row, mode_snapshot(enabled[1], row.scope_id)):
                row.status, row.action, row.run_token = "cancelled", "end", None
                row.not_before = row.wake_at = None
                row.revision += 1
                row.updated_at = moment
                continue
            restored.append(_snapshot(*enabled, row))
        return tuple(restored)


async def read_schedule(mode: ModeSnapshot, *, session_factory: Any = None) -> DialogueSchedule | None:
    async with session(session_factory) as db:
        enabled = await _enabled(db, mode.scope_id)
        row = await db.get(CompanionDialogue, mode.scope_id)
        if enabled is None or row is None or not _matches(row, mode):
            return None
        if enabled[1].revision != mode.revision or enabled[1].persona_key != mode.persona_key:
            return None
        return _snapshot(*enabled, row)


async def begin(mode: ModeSnapshot, *, now: datetime, session_factory: Any = None) -> str | None:
    """Consume any old alarm before entering model/transport code."""
    moment = naive(now)
    async with transaction(session_factory) as db:
        enabled = await _enabled(db, mode.scope_id)
        if enabled is None or enabled[1].revision != mode.revision or enabled[1].persona_key != mode.persona_key:
            return None
        row = await db.get(CompanionDialogue, mode.scope_id)
        if row is not None and row.status == "running":
            return None
        if row is None:
            row = CompanionDialogue(
                scope_id=mode.scope_id,
                persona_key=mode.persona_key,
                mode_revision=mode.revision,
                revision=0,
                updated_at=moment,
            )
            db.add(row)
        row.persona_key, row.mode_revision = mode.persona_key, mode.revision
        row.revision += 1
        row.status, row.action = "running", "end"
        row.not_before = row.wake_at = None
        row.run_token, row.updated_at = token("dialogue"), moment
        return row.run_token


async def finish(
    mode: ModeSnapshot,
    run_token: str,
    decision: DialogueDecision,
    *,
    status: str,
    confirmed_deliveries: int,
    now: datetime,
    session_factory: Any = None,
    cancelled: Callable[[], bool] | None = None,
) -> DialogueSchedule | None:
    if status not in {"completed", "unknown", "failed", "cancelled"}:
        raise ValueError("Invalid dialogue outcome status")
    if isinstance(confirmed_deliveries, bool) or not isinstance(confirmed_deliveries, int) or confirmed_deliveries < 0:
        raise ValueError("Invalid confirmed dialogue delivery count")
    moment = naive(now)
    async with transaction(session_factory) as db:
        row = await db.get(CompanionDialogue, mode.scope_id)
        if row is None or row.run_token != run_token or row.status != "running" or not _matches(row, mode):
            return None
        enabled = await _enabled(db, mode.scope_id)
        if (cancelled is not None and cancelled()) or (
            enabled is None or enabled[1].revision != mode.revision or enabled[1].persona_key != mode.persona_key
        ):
            status = "cancelled"
        row.revision += 1
        row.run_token, row.updated_at = None, moment
        row.confirmed_deliveries += confirmed_deliveries
        row.action = decision.action if status == "completed" else "end"
        row.status = (
            "scheduled"
            if status == "completed" and decision.action != "end"
            else ("idle" if status == "completed" else status)
        )
        deadlines = decision_deadlines(decision, now) if status == "completed" else (None, None)
        row.not_before, row.wake_at = (naive(value) if value is not None else None for value in deadlines)
        return _snapshot(*enabled, row) if enabled else None


async def cancel(
    scope_id: int,
    *,
    now: datetime,
    session_factory: Any = None,
    expected: DialogueSchedule | None = None,
) -> None:
    async with transaction(session_factory) as db:
        row = await db.get(CompanionDialogue, scope_id)
        if row is not None:
            if expected is not None and (
                row.revision != expected.revision
                or row.run_token != expected.run_token
                or not _matches(row, expected.mode)
            ):
                return
            row.revision += 1
            row.status, row.action, row.run_token = "cancelled", "end", None
            row.not_before = row.wake_at = None
            row.updated_at = naive(now)
