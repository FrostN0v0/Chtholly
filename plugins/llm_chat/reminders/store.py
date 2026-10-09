"""Transactional persistence for durable reminder tasks.

This module deliberately owns all reminder-specific ORM I/O.  Callers pass
already-validated, timezone-aware UTC datetimes; the database stores UTC as
naive values, matching the rest of llm_chat's SQLite schema.
"""

from __future__ import annotations

import json
from typing import Any
import asyncio
from secrets import token_hex
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
from collections.abc import Iterable

from sqlalchemy import or_, func, text, exists, select, update
from sqlalchemy.exc import IntegrityError
from entari_plugin_database import get_session

from ..models import ReminderTask, ReminderMutation, ReminderOccurrence, ReminderPreference

_claim_locks: dict[Any, asyncio.Lock] = {}


def _serialized_claim(function):
    async def wrapped(*args, **kwargs):
        lock = _claim_locks.setdefault(asyncio.get_running_loop(), asyncio.Lock())
        async with lock:
            return await function(*args, **kwargs)

    return wrapped


MAX_GOAL = 1_000
MAX_BACKGROUND = 3_000
MAX_CONTINUATION = 2_000
MAX_REASON = 1_000
MAX_OUTCOME = 1_000
MAX_OPERATION_ID = 200
MAX_TIMEZONE = 64
MAX_RECEIPT_ID = 200
CLAIM_STATES = ("claimed", "model_started", "dm_started", "transport_started", "confirmed")
FINAL_STATES = ("completed", "paused", "cancelled", "expired_recovered", "unknown")


class ReminderError(RuntimeError):
    """Base class for reminder store failures."""


class ReminderNotFound(ReminderError):
    """The requested task or claim does not exist for its owner."""


class ReminderConflict(ReminderError):
    """A revision, lease token, or lifecycle precondition no longer matches."""


@dataclass(frozen=True, slots=True)
class ReminderClaim:
    """Immutable capability returned by :func:`try_claim_due`."""

    task_ref: str
    lease_token: str
    revision: int
    occurrence_sequence: int
    lease_deadline: datetime

    @property
    def sequence(self) -> int:
        return self.occurrence_sequence


def _bounded(value: str, limit: int, field: str, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    if required and not value.strip():
        raise ValueError(f"{field} is required")
    if len(value) > limit:
        raise ValueError(f"{field} exceeds {limit} characters")
    return value


def _operation_id(value: str) -> str:
    return _bounded(value, MAX_OPERATION_ID, "operation_id", required=True)


def _utc_naive(value: datetime, field: str = "datetime") -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _aware_utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _now_naive(value: datetime | None) -> datetime:
    return _utc_naive(value, "now") if value is not None else datetime.utcnow()


def _new_token(prefix: str) -> str:
    return f"{prefix}_{token_hex(16)}"


def _owner_clause(
    task: Any,
    *,
    platform: str | None,
    account_id: str,
    unified_user_id: str,
    platform_user_id: str | None = None,
):
    clauses = [task.account_id == account_id, task.unified_user_id == unified_user_id]
    if platform is not None:
        clauses.append(task.platform == platform)
    if platform_user_id is not None:
        clauses.append(task.platform_user_id == platform_user_id)
    return clauses


async def _mutation(db: Any, operation_id: str) -> ReminderMutation | None:
    return await db.scalar(select(ReminderMutation).where(ReminderMutation.operation_id == operation_id))


async def _task_for_owner(
    db: Any,
    task_ref: str,
    *,
    platform: str | None,
    account_id: str,
    unified_user_id: str,
    platform_user_id: str | None = None,
) -> ReminderTask | None:
    return await db.scalar(
        select(ReminderTask).where(
            ReminderTask.task_ref == task_ref,
            *_owner_clause(
                ReminderTask,
                platform=platform,
                account_id=account_id,
                unified_user_id=unified_user_id,
                platform_user_id=platform_user_id,
            ),
        )
    )


async def create_task(
    *,
    operation_id: str,
    platform: str,
    account_id: str,
    unified_user_id: str,
    due_at: datetime,
    goal: str,
    timezone_name: str = "UTC",
    mode: str = "once",
    origin: str = "user",
    platform_user_id: str = "",
    source_scope_id: int | None = None,
    source_channel_id: str = "",
    delivery_target_type: str = "channel",
    delivery_channel_id: str = "",
    delivery_user_id: str = "",
    dm_authorized: bool = False,
    background: str = "",
    continuation_state: str = "",
    source_turn_ref: str = "",
    source_event_ref: str = "",
    source_execution_ref: str | None = None,
    expires_at: datetime | None = None,
) -> ReminderTask:
    """Create one task and its mutation ledger entry atomically.

    Repeating the same operation ID, or the same non-empty source execution
    reference for this account, returns the original task without replaying a
    write.  A caller must retain the operation ID when a commit response is
    uncertain and query through this function again.
    """

    op = _operation_id(operation_id)
    due = _utc_naive(due_at, "due_at")
    expiry = _utc_naive(expires_at, "expires_at") if expires_at is not None else None
    if expiry is not None and expiry <= due:
        raise ValueError("expires_at must be after due_at")
    platform = _bounded(platform, 100, "platform", required=True)
    account_id = _bounded(account_id, 300, "account_id", required=True)
    unified_user_id = _bounded(unified_user_id, 300, "unified_user_id", required=True)
    platform_user_id = _bounded(platform_user_id, 300, "platform_user_id")
    timezone_name = _bounded(timezone_name, MAX_TIMEZONE, "timezone", required=True)
    goal = _bounded(goal, MAX_GOAL, "goal", required=True)
    background = _bounded(background, MAX_BACKGROUND, "background")
    continuation_state = _bounded(continuation_state, MAX_CONTINUATION, "continuation_state")
    source_turn_ref = _bounded(source_turn_ref, 300, "source_turn_ref")
    source_event_ref = _bounded(source_event_ref, 300, "source_event_ref")
    source_execution_ref = (
        _bounded(source_execution_ref, 300, "source_execution_ref") if source_execution_ref is not None else None
    )
    source_channel_id = _bounded(source_channel_id, 300, "source_channel_id")
    delivery_target_type = _bounded(delivery_target_type, 32, "delivery_target_type", required=True)
    delivery_channel_id = _bounded(delivery_channel_id, 300, "delivery_channel_id")
    delivery_user_id = _bounded(delivery_user_id, 300, "delivery_user_id")
    mode = _bounded(mode, 32, "mode", required=True)
    origin = _bounded(origin, 32, "origin", required=True)

    async with get_session() as db:
        if db.get_bind().dialect.name == "sqlite":
            await db.execute(text("BEGIN IMMEDIATE"))
        existing = await _mutation(db, op)
        if existing is not None:
            if not existing.task_ref:
                raise ReminderConflict("create mutation has no task reference")
            task = await db.scalar(select(ReminderTask).where(ReminderTask.task_ref == existing.task_ref))
            if task is None:
                raise ReminderConflict("create mutation refers to a missing task")
            return task

        # The source execution reference is an additional stable idempotency
        # key.  Empty references are intentionally not deduplicated.
        if source_execution_ref:
            existing_task = await db.scalar(
                select(ReminderTask).where(
                    ReminderTask.platform == platform,
                    ReminderTask.account_id == account_id,
                    ReminderTask.source_execution_ref == source_execution_ref,
                )
            )
            if existing_task is not None:
                db.add(
                    ReminderMutation(
                        operation_id=op,
                        task_ref=existing_task.task_ref,
                        action="create",
                        revision=existing_task.revision,
                        outcome="already_submitted",
                    )
                )
                try:
                    await db.commit()
                except IntegrityError:
                    await db.rollback()
                    mutation = await _mutation(db, op)
                    if mutation is None:
                        raise
                return existing_task

        active_count = await db.scalar(
            select(func.count(ReminderTask.id)).where(
                ReminderTask.platform == platform,
                ReminderTask.account_id == account_id,
                ReminderTask.unified_user_id == unified_user_id,
                ReminderTask.status == "active",
            )
        )
        if active_count >= 20:
            raise ReminderConflict("at most 20 active reminders per account and user")

        task = ReminderTask(
            platform=platform,
            account_id=account_id,
            source_scope_id=source_scope_id,
            source_channel_id=source_channel_id,
            unified_user_id=unified_user_id,
            platform_user_id=platform_user_id,
            delivery_target_type=delivery_target_type,
            delivery_channel_id=delivery_channel_id,
            delivery_user_id=delivery_user_id,
            dm_authorized=bool(dm_authorized),
            origin=origin,
            mode=mode,
            goal=goal,
            background=background,
            continuation_state=continuation_state,
            source_turn_ref=source_turn_ref,
            source_event_ref=source_event_ref,
            source_execution_ref=source_execution_ref,
            timezone=timezone_name,
            due_at=due,
            expires_at=expiry,
        )
        db.add(task)
        await db.flush()
        db.add(
            ReminderMutation(
                operation_id=op,
                task_ref=task.task_ref,
                action="create",
                revision=task.revision,
                outcome="created",
            )
        )
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            mutation = await _mutation(db, op)
            if mutation is None:
                raise
            found = await db.scalar(select(ReminderTask).where(ReminderTask.task_ref == mutation.task_ref))
            if found is None:
                raise ReminderConflict("create mutation refers to a missing task")
            return found
        return task


async def get_task_for_service(task_ref: str) -> ReminderTask | None:
    """Fetch one opaque task for the trusted scheduler, not for model tools."""
    async with get_session() as db:
        return await db.scalar(select(ReminderTask).where(ReminderTask.task_ref == task_ref))


async def expire_tasks() -> int:
    """End explicitly expired unclaimed tasks without generating a synthetic wakeup."""
    now = datetime.utcnow()
    busy = exists(
        select(ReminderOccurrence.id).where(
            ReminderOccurrence.task_ref == ReminderTask.task_ref,
            ReminderOccurrence.state.in_(CLAIM_STATES),
        )
    )
    async with get_session() as db, db.begin():
        result = await db.execute(
            update(ReminderTask)
            .where(
                ReminderTask.status == "active",
                ReminderTask.expires_at.is_not(None),
                ReminderTask.expires_at <= now,
                ~busy,
            )
            .values(status="completed", revision=ReminderTask.revision + 1, updated_at=now)
        )
        return result.rowcount


async def due_task_refs(*, platform: str, account_id: str, limit: int = 20) -> list[str]:
    """Inspect due work without claiming offline accounts or opening a long transaction."""
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    async with get_session() as db:
        return list(
            (
                await db.execute(
                    select(ReminderTask.task_ref)
                    .where(
                        ReminderTask.platform == platform,
                        ReminderTask.account_id == account_id,
                        ReminderTask.status == "active",
                        ReminderTask.due_at <= datetime.utcnow(),
                        or_(ReminderTask.expires_at.is_(None), ReminderTask.expires_at > datetime.utcnow()),
                    )
                    .order_by(ReminderTask.due_at, ReminderTask.id)
                    .limit(limit)
                )
            ).scalars()
        )


async def get_occurrence(claim: ReminderClaim) -> ReminderOccurrence | None:
    async with get_session() as db:
        return await db.scalar(
            select(ReminderOccurrence).where(
                ReminderOccurrence.task_ref == claim.task_ref,
                ReminderOccurrence.sequence == claim.occurrence_sequence,
            )
        )


async def get_wakeup_history(task_ref: str) -> tuple[datetime, ...]:
    """Read reached occurrence times for autonomous scheduling limits."""
    async with get_session() as db:
        values = list(
            (
                await db.execute(
                    select(ReminderOccurrence.scheduled_at)
                    .where(
                        ReminderOccurrence.task_ref == task_ref,
                        ReminderOccurrence.state != "expired_recovered",
                    )
                    .order_by(ReminderOccurrence.sequence.asc())
                )
            ).scalars()
        )
    return tuple(value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value for value in values)


async def renew_claim(claim: ReminderClaim, *, lease_seconds: int = 300) -> bool:
    """Extend a still-owned lease with one CAS; never renew a revoked or expired claim."""
    if not 1 <= lease_seconds <= 86_400:
        raise ValueError("invalid lease_seconds")
    now = datetime.utcnow()
    async with get_session() as db:
        result = await db.execute(
            update(ReminderOccurrence)
            .where(
                ReminderOccurrence.task_ref == claim.task_ref,
                ReminderOccurrence.sequence == claim.occurrence_sequence,
                ReminderOccurrence.lease_token == claim.lease_token,
                ReminderOccurrence.lease_deadline > now,
                ReminderOccurrence.state.in_(CLAIM_STATES),
                exists(
                    select(ReminderTask.id).where(
                        ReminderTask.task_ref == claim.task_ref,
                        ReminderTask.status == "active",
                        ReminderTask.revision == claim.revision,
                    )
                ),
            )
            .values(lease_deadline=now + timedelta(seconds=lease_seconds))
        )
        await db.commit()
        return result.rowcount == 1


async def get_agent_turn_ref(turn_id: int) -> str:
    from ..models import AgentTurn

    async with get_session() as db:
        ref = await db.scalar(select(AgentTurn.turn_ref).where(AgentTurn.id == turn_id))
        if not ref:
            raise ReminderConflict("scheduled AgentTurn was not persisted")
        return str(ref)


async def bind_turn_ref(claim: ReminderClaim, turn_ref: str) -> bool:
    """Associate the real scheduled turn only while this lease still owns it."""
    now = datetime.utcnow()
    async with get_session() as db:
        result = await db.execute(
            update(ReminderOccurrence)
            .where(
                ReminderOccurrence.task_ref == claim.task_ref,
                ReminderOccurrence.sequence == claim.occurrence_sequence,
                ReminderOccurrence.lease_token == claim.lease_token,
                ReminderOccurrence.lease_deadline > now,
                ReminderOccurrence.state.in_(CLAIM_STATES),
                exists(
                    select(ReminderTask.id).where(
                        ReminderTask.task_ref == claim.task_ref,
                        ReminderTask.status == "active",
                        ReminderTask.revision == claim.revision,
                    )
                ),
            )
            .values(turn_ref=turn_ref[:300])
        )
        await db.commit()
        return result.rowcount == 1


async def get_task_guild_id(source_scope_id: int | None) -> str:
    if source_scope_id is None:
        return ""
    from ..models import ChatScope

    async with get_session() as db:
        scope = await db.get(ChatScope, source_scope_id)
        return str(scope.guild_id or "") if scope is not None else ""


async def get_task(
    task_ref: str,
    *,
    platform: str | None = None,
    account_id: str,
    unified_user_id: str,
    platform_user_id: str | None = None,
) -> ReminderTask | None:
    async with get_session() as db:
        return await _task_for_owner(
            db,
            task_ref,
            platform=platform,
            account_id=account_id,
            unified_user_id=unified_user_id,
            platform_user_id=platform_user_id,
        )


async def list_tasks(
    *,
    account_id: str,
    unified_user_id: str,
    platform: str | None = None,
    platform_user_id: str | None = None,
    statuses: Iterable[str] | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[ReminderTask]:
    if limit < 1 or limit > 500:
        raise ValueError("limit must be between 1 and 500")
    if offset < 0:
        raise ValueError("offset must not be negative")
    async with get_session() as db:
        clauses = _owner_clause(
            ReminderTask,
            platform=platform,
            account_id=account_id,
            unified_user_id=unified_user_id,
            platform_user_id=platform_user_id,
        )
        if statuses is not None:
            values = tuple(statuses)
            if not values:
                return []
            clauses.append(ReminderTask.status.in_(values))
        result = await db.execute(
            select(ReminderTask)
            .where(*clauses)
            .order_by(ReminderTask.due_at.asc(), ReminderTask.id.asc())
            .offset(offset)
            .limit(limit)
        )
        return list(result.scalars().all())


async def _mutate_task(
    task_ref: str,
    *,
    operation_id: str,
    action: str,
    expected_revision: int,
    account_id: str,
    unified_user_id: str,
    platform: str | None,
    platform_user_id: str | None,
    status: str | None = None,
    reason: str = "",
    due_at: datetime | None = None,
    expires_at: datetime | None = None,
    goal: str | None = None,
    background: str | None = None,
    continuation_state: str | None = None,
    timezone_name: str | None = None,
) -> ReminderTask:
    op = _operation_id(operation_id)
    reason = _bounded(reason, MAX_REASON, "reason")
    values: dict[str, Any] = {"revision": ReminderTask.revision + 1, "updated_at": datetime.utcnow()}
    if status is not None:
        values["status"] = status
        if status == "paused":
            values["paused_reason"] = reason
        elif status in ("active", "completed", "cancelled"):
            values["paused_reason"] = ""
    if due_at is not None:
        values["due_at"] = _utc_naive(due_at, "due_at")
    if expires_at is not None:
        values["expires_at"] = _utc_naive(expires_at, "expires_at")
    if goal is not None:
        values["goal"] = _bounded(goal, MAX_GOAL, "goal", required=True)
    if background is not None:
        values["background"] = _bounded(background, MAX_BACKGROUND, "background")
    if continuation_state is not None:
        values["continuation_state"] = _bounded(continuation_state, MAX_CONTINUATION, "continuation_state")
    if timezone_name is not None:
        values["timezone"] = _bounded(timezone_name, MAX_TIMEZONE, "timezone", required=True)

    async with get_session() as db:
        if db.get_bind().dialect.name == "sqlite":
            await db.execute(text("BEGIN IMMEDIATE"))
        existing_mutation = await _mutation(db, op)
        if existing_mutation is not None:
            found = await db.scalar(select(ReminderTask).where(ReminderTask.task_ref == existing_mutation.task_ref))
            if found is None:
                raise ReminderConflict("mutation refers to a missing task")
            if not _owner_matches(found, platform, account_id, unified_user_id, platform_user_id):
                raise ReminderNotFound("task is not owned by this caller")
            return found

        task = await _task_for_owner(
            db,
            task_ref,
            platform=platform,
            account_id=account_id,
            unified_user_id=unified_user_id,
            platform_user_id=platform_user_id,
        )
        if task is None:
            raise ReminderNotFound("task not found")
        effective_due = _utc_naive(due_at, "due_at") if due_at is not None else task.due_at
        effective_expiry = _utc_naive(expires_at, "expires_at") if expires_at is not None else task.expires_at
        if effective_expiry is not None and effective_expiry <= effective_due:
            raise ValueError("expires_at must be after due_at")
        if action == "resume":
            if task.status != "paused":
                raise ReminderConflict("only paused reminders can be resumed")
            latest = await db.scalar(
                select(ReminderOccurrence)
                .where(ReminderOccurrence.task_ref == task_ref)
                .order_by(ReminderOccurrence.sequence.desc())
                .limit(1)
            )
            if (
                task.due_at <= datetime.utcnow()
                and latest is not None
                and (
                    latest.confirmed_count > 0
                    or latest.transport_started
                    or latest.dm_started
                    or latest.state == "unknown"
                )
            ):
                raise ReminderConflict("reschedule to a future time before resuming uncertain or confirmed work")
        if status == "active" and task.status != "active":
            active_count = await db.scalar(
                select(func.count(ReminderTask.id)).where(
                    ReminderTask.platform == task.platform,
                    ReminderTask.account_id == account_id,
                    ReminderTask.unified_user_id == unified_user_id,
                    ReminderTask.status == "active",
                )
            )
            if active_count >= 20:
                raise ReminderConflict("at most 20 active reminders per account and user")
        uncertain_update = False
        if action == "update":
            uncertain_update = bool(
                await db.scalar(
                    select(ReminderOccurrence.id)
                    .where(
                        ReminderOccurrence.task_ref == task_ref,
                        ReminderOccurrence.state.in_(CLAIM_STATES),
                        ReminderOccurrence.transport_started.is_(True),
                    )
                    .limit(1)
                )
            )
            if uncertain_update:
                values["status"] = "paused"
                values["paused_reason"] = "occurrence_result_unknown"
        result = await db.execute(
            update(ReminderTask)
            .where(ReminderTask.id == task.id, ReminderTask.revision == expected_revision)
            .values(**values)
        )
        if result.rowcount != 1:
            raise ReminderConflict("stale reminder revision")
        if status in ("cancelled", "paused") or action == "update":
            terminal = "cancelled" if status == "cancelled" else "paused"
            reason = (
                "task_cancelled" if status == "cancelled" else "task_paused" if status == "paused" else "task_updated"
            )
            for transport_started in (False, True):
                outcome = {
                    "state": terminal,
                    "lease_deadline": None,
                    "failure_reason": reason if not transport_started else f"transport_result_unknown_after_{reason}",
                    "finished_at": datetime.utcnow(),
                }
                if not transport_started:
                    outcome["lease_token"] = None
                await db.execute(
                    update(ReminderOccurrence)
                    .where(
                        ReminderOccurrence.task_ref == task_ref,
                        ReminderOccurrence.state.in_(CLAIM_STATES),
                        ReminderOccurrence.transport_started.is_(transport_started),
                    )
                    .values(**outcome)
                )
        new_revision = expected_revision + 1
        db.add(
            ReminderMutation(
                operation_id=op,
                task_ref=task_ref,
                action=action,
                revision=new_revision,
                outcome=status or "updated",
            )
        )
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            mutation = await _mutation(db, op)
            if mutation is None:
                raise
        found = await db.scalar(select(ReminderTask).where(ReminderTask.task_ref == task_ref))
        if found is None:
            raise ReminderConflict("task disappeared after mutation")
        return found


def _owner_matches(
    task: ReminderTask, platform: str | None, account_id: str, unified_user_id: str, platform_user_id: str | None
) -> bool:
    return (
        task.account_id == account_id
        and task.unified_user_id == unified_user_id
        and (platform is None or task.platform == platform)
        and (platform_user_id is None or task.platform_user_id == platform_user_id)
    )


async def update_task(
    task_ref: str,
    *,
    operation_id: str,
    expected_revision: int,
    account_id: str,
    unified_user_id: str,
    platform: str | None = None,
    platform_user_id: str | None = None,
    goal: str | None = None,
    background: str | None = None,
    continuation_state: str | None = None,
    timezone_name: str | None = None,
    due_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> ReminderTask:
    return await _mutate_task(
        task_ref,
        operation_id=operation_id,
        action="update",
        expected_revision=expected_revision,
        account_id=account_id,
        unified_user_id=unified_user_id,
        platform=platform,
        platform_user_id=platform_user_id,
        goal=goal,
        background=background,
        continuation_state=continuation_state,
        timezone_name=timezone_name,
        due_at=due_at,
        expires_at=expires_at,
    )


async def cancel_task(
    task_ref: str,
    *,
    operation_id: str,
    expected_revision: int,
    account_id: str,
    unified_user_id: str,
    platform: str | None = None,
    platform_user_id: str | None = None,
    reason: str = "cancelled_by_user",
) -> ReminderTask:
    return await _mutate_task(
        task_ref,
        operation_id=operation_id,
        action="cancel",
        expected_revision=expected_revision,
        account_id=account_id,
        unified_user_id=unified_user_id,
        platform=platform,
        platform_user_id=platform_user_id,
        status="cancelled",
        reason=reason,
    )


async def pause_task(
    task_ref: str,
    *,
    operation_id: str,
    expected_revision: int,
    account_id: str,
    unified_user_id: str,
    platform: str | None = None,
    platform_user_id: str | None = None,
    reason: str = "paused_by_user",
) -> ReminderTask:
    return await _mutate_task(
        task_ref,
        operation_id=operation_id,
        action="pause",
        expected_revision=expected_revision,
        account_id=account_id,
        unified_user_id=unified_user_id,
        platform=platform,
        platform_user_id=platform_user_id,
        status="paused",
        reason=reason,
    )


async def resume_task(
    task_ref: str,
    *,
    operation_id: str,
    expected_revision: int,
    account_id: str,
    unified_user_id: str,
    platform: str | None = None,
    platform_user_id: str | None = None,
) -> ReminderTask:
    return await _mutate_task(
        task_ref,
        operation_id=operation_id,
        action="resume",
        expected_revision=expected_revision,
        account_id=account_id,
        unified_user_id=unified_user_id,
        platform=platform,
        platform_user_id=platform_user_id,
        status="active",
    )


async def set_preference(
    *,
    operation_id: str,
    account_id: str,
    unified_user_id: str,
    enabled: bool,
) -> ReminderPreference:
    op = _operation_id(operation_id)
    account_id = _bounded(account_id, 300, "account_id", required=True)
    unified_user_id = _bounded(unified_user_id, 300, "unified_user_id", required=True)
    async with get_session() as db:
        mutation = await _mutation(db, op)
        if mutation is not None:
            pref = await db.get(ReminderPreference, (account_id, unified_user_id))
            if pref is None:
                raise ReminderConflict("preference mutation refers to a missing preference")
            return pref
        pref = await db.get(ReminderPreference, (account_id, unified_user_id))
        if pref is None:
            pref = ReminderPreference(account_id=account_id, unified_user_id=unified_user_id, enabled=bool(enabled))
            db.add(pref)
        else:
            pref.enabled = bool(enabled)
            pref.updated_at = datetime.utcnow()
        db.add(
            ReminderMutation(
                operation_id=op,
                task_ref=None,
                action="preference",
                revision=0,
                outcome="enabled" if enabled else "disabled",
            )
        )
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            mutation = await _mutation(db, op)
            if mutation is None:
                raise
        return await db.get(ReminderPreference, (account_id, unified_user_id))


async def get_preference(*, account_id: str, unified_user_id: str) -> ReminderPreference | None:
    async with get_session() as db:
        return await db.get(ReminderPreference, (account_id, unified_user_id))


async def _recover_stale_in_session(db: Any, now: datetime) -> int:
    rows = list(
        (
            await db.execute(
                select(ReminderOccurrence).where(
                    ReminderOccurrence.lease_deadline.is_not(None),
                    ReminderOccurrence.lease_deadline < now,
                    ReminderOccurrence.state.in_(CLAIM_STATES),
                )
            )
        ).scalars()
    )
    recovered = 0
    for occurrence in rows:
        task = await db.scalar(select(ReminderTask).where(ReminderTask.task_ref == occurrence.task_ref))
        if task is None:
            occurrence.state = "unknown"
            occurrence.failure_reason = "task_missing"
            occurrence.finished_at = now
            occurrence.lease_token = None
            occurrence.lease_deadline = None
            recovered += 1
            continue
        if task.status != "active":
            occurrence.state = "cancelled" if task.status == "cancelled" else "paused"
            occurrence.failure_reason = "task_not_active"
            occurrence.finished_at = now
            occurrence.lease_token = None
            occurrence.lease_deadline = None
            recovered += 1
            continue
        if occurrence.model_started or occurrence.dm_started or occurrence.transport_started:
            occurrence.state = "unknown"
            occurrence.failure_reason = "lease_expired_after_external_boundary"
            occurrence.finished_at = now
            occurrence.lease_token = None
            occurrence.lease_deadline = None
            task.status = "paused"
            task.paused_reason = "occurrence_result_unknown"
            task.revision += 1
            task.updated_at = now
        else:
            occurrence.state = "expired_recovered"
            occurrence.failure_reason = "lease_expired_before_model"
            occurrence.finished_at = now
            occurrence.lease_token = None
            occurrence.lease_deadline = None
            task.due_at = now
            task.revision += 1
            task.updated_at = now
        recovered += 1
    return recovered


async def recover_stale(*, now: datetime | None = None) -> int:
    now_naive = _now_naive(now)
    async with get_session() as db, db.begin():
        count = await _recover_stale_in_session(db, now_naive)
        return count


@_serialized_claim
async def try_claim_due(
    task_ref: str | None = None,
    now: datetime | None = None,
    lease_seconds: int = 300,
    *,
    platform: str | None = None,
    account_id: str | None = None,
    unified_user_id: str | None = None,
) -> ReminderClaim | None:
    """Atomically claim one due task, returning an immutable lease capability."""

    if lease_seconds < 1 or lease_seconds > 86_400:
        raise ValueError("lease_seconds must be between 1 and 86400")
    now_naive = _now_naive(now)
    deadline = now_naive + timedelta(seconds=lease_seconds)
    async with get_session() as db:
        if db.get_bind().dialect.name == "sqlite":
            await db.execute(text("BEGIN IMMEDIATE"))
        # Recovery is a separate maintenance operation; claims stay short and read/CAS-only.
        active_occurrence = exists(
            select(ReminderOccurrence.id).where(
                ReminderOccurrence.task_ref == ReminderTask.task_ref,
                ReminderOccurrence.state.in_(CLAIM_STATES),
                or_(ReminderOccurrence.lease_deadline.is_(None), ReminderOccurrence.lease_deadline >= now_naive),
            )
        )
        clauses = [
            ReminderTask.status == "active",
            ReminderTask.due_at <= now_naive,
            or_(ReminderTask.expires_at.is_(None), ReminderTask.expires_at > now_naive),
            ~active_occurrence,
        ]
        if task_ref is not None:
            clauses.append(ReminderTask.task_ref == task_ref)
        if platform is not None:
            clauses.append(ReminderTask.platform == platform)
        if account_id is not None:
            clauses.append(ReminderTask.account_id == account_id)
        if unified_user_id is not None:
            clauses.append(ReminderTask.unified_user_id == unified_user_id)
        task = await db.scalar(
            select(ReminderTask).where(*clauses).order_by(ReminderTask.due_at.asc(), ReminderTask.id.asc()).limit(1)
        )
        if task is None:
            await db.commit()
            return None
        old_revision = task.revision
        new_sequence = task.current_occurrence + 1
        result = await db.execute(
            update(ReminderTask)
            .where(ReminderTask.id == task.id, ReminderTask.revision == old_revision, ReminderTask.status == "active")
            .values(
                revision=old_revision + 1,
                current_occurrence=new_sequence,
                updated_at=now_naive,
            )
        )
        if result.rowcount != 1:
            await db.rollback()
            return None
        token = _new_token("lease")
        occurrence = ReminderOccurrence(
            task_ref=task.task_ref,
            sequence=new_sequence,
            task_revision=old_revision + 1,
            lease_token=token,
            lease_deadline=deadline,
            scheduled_at=task.due_at,
            started_at=now_naive,
            state="claimed",
        )
        db.add(occurrence)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            return None
        return ReminderClaim(
            task.task_ref, token, old_revision + 1, new_sequence, deadline.replace(tzinfo=timezone.utc)
        )


def _claim_parts(
    claim_or_task_ref: ReminderClaim | str,
    occurrence_sequence: int | None,
    lease_token: str | None,
    expected_revision: int | None,
) -> tuple[str, int, str, int]:
    if isinstance(claim_or_task_ref, ReminderClaim):
        return (
            claim_or_task_ref.task_ref,
            claim_or_task_ref.occurrence_sequence,
            claim_or_task_ref.lease_token,
            claim_or_task_ref.revision,
        )
    if occurrence_sequence is None or not lease_token or expected_revision is None:
        raise ValueError("claim, occurrence_sequence, lease_token and expected_revision are required")
    return claim_or_task_ref, occurrence_sequence, lease_token, expected_revision


async def record_model_started(
    claim_or_task_ref: ReminderClaim | str,
    occurrence_sequence: int | None = None,
    lease_token: str | None = None,
    expected_revision: int | None = None,
    *,
    now: datetime | None = None,
) -> bool:
    task_ref, sequence, token, revision = _claim_parts(
        claim_or_task_ref, occurrence_sequence, lease_token, expected_revision
    )
    stamp = _now_naive(now)
    async with get_session() as db:
        owned = exists(
            select(ReminderTask.id).where(
                ReminderTask.task_ref == task_ref,
                ReminderTask.revision == revision,
                ReminderTask.status == "active",
            )
        )
        result = await db.execute(
            update(ReminderOccurrence)
            .where(
                ReminderOccurrence.task_ref == task_ref,
                ReminderOccurrence.sequence == sequence,
                ReminderOccurrence.lease_token == token,
                ReminderOccurrence.state.in_(CLAIM_STATES),
                ReminderOccurrence.model_started.is_(False),
                owned,
            )
            .values(model_started=True, model_started_at=stamp, state="model_started")
        )
        if result.rowcount == 1:
            await db.commit()
            return True
        already = await db.scalar(
            select(ReminderOccurrence.id).where(
                ReminderOccurrence.task_ref == task_ref,
                ReminderOccurrence.sequence == sequence,
                ReminderOccurrence.lease_token == token,
                ReminderOccurrence.model_started.is_(True),
                ReminderOccurrence.state.in_(CLAIM_STATES),
                owned,
            )
        )
        await db.commit()
        return already is not None


async def record_external_started(
    claim_or_task_ref: ReminderClaim | str,
    occurrence_sequence: int | None = None,
    lease_token: str | None = None,
    expected_revision: int | None = None,
    *,
    kind: str = "transport",
    now: datetime | None = None,
) -> bool:
    if kind not in {"dm", "transport"}:
        raise ValueError("kind must be dm or transport")
    task_ref, sequence, token, revision = _claim_parts(
        claim_or_task_ref, occurrence_sequence, lease_token, expected_revision
    )
    stamp = _now_naive(now)
    async with get_session() as db:
        values = (
            {
                "dm_started": True,
                "dm_started_at": func.coalesce(ReminderOccurrence.dm_started_at, stamp),
                "state": "dm_started",
            }
            if kind == "dm"
            else {
                "transport_started": True,
                "transport_started_at": func.coalesce(ReminderOccurrence.transport_started_at, stamp),
                "state": "transport_started",
            }
        )
        result = await db.execute(
            update(ReminderOccurrence)
            .where(
                ReminderOccurrence.task_ref == task_ref,
                ReminderOccurrence.sequence == sequence,
                ReminderOccurrence.lease_token == token,
                ReminderOccurrence.state.in_(CLAIM_STATES),
                exists(
                    select(ReminderTask.id).where(
                        ReminderTask.task_ref == task_ref,
                        ReminderTask.revision == revision,
                        ReminderTask.status == "active",
                    )
                ),
            )
            .values(**values)
        )
        await db.commit()
        return result.rowcount == 1


async def record_confirmed(
    claim_or_task_ref: ReminderClaim | str,
    occurrence_sequence: int | None = None,
    lease_token: str | None = None,
    expected_revision: int | None = None,
    *,
    receipt_id: str = "",
    now: datetime | None = None,
) -> bool:
    task_ref, sequence, token, revision = _claim_parts(
        claim_or_task_ref, occurrence_sequence, lease_token, expected_revision
    )
    receipt_id = _bounded(receipt_id, MAX_RECEIPT_ID, "receipt_id")
    stamp = _now_naive(now)
    async with get_session() as db:
        sqlite = db.get_bind().dialect.name == "sqlite"
        if sqlite:
            await db.execute(text("BEGIN IMMEDIATE"))
        task = await db.scalar(select(ReminderTask).where(ReminderTask.task_ref == task_ref))
        statement = select(ReminderOccurrence).where(
            ReminderOccurrence.task_ref == task_ref,
            ReminderOccurrence.sequence == sequence,
            ReminderOccurrence.lease_token == token,
        )
        occurrence = await db.scalar(statement if sqlite else statement.with_for_update())
        active = task is not None and task.revision == revision and task.status == "active"
        interrupted = (
            task is not None
            and task.status in {"cancelled", "paused"}
            and occurrence is not None
            and occurrence.transport_started
            and occurrence.state == task.status
        )
        if occurrence is None or not ((active and occurrence.state in CLAIM_STATES) or interrupted):
            return False
        try:
            receipts = json.loads(occurrence.confirmed_receipts_json or "[]")
        except (TypeError, ValueError):
            receipts = []
        if not isinstance(receipts, list):
            receipts = []
        if receipt_id and receipt_id in receipts:
            await db.commit()
            return True
        if receipt_id:
            receipts.append(receipt_id)
        values = {
            "confirmed_count": ReminderOccurrence.confirmed_count + 1,
            "confirmed_receipts_json": json.dumps(receipts[-32:], ensure_ascii=False),
        }
        if active:
            values.update(state="confirmed", failure_reason="", lease_deadline=occurrence.lease_deadline or stamp)
        elif interrupted:
            values["failure_reason"] = f"{task.status}_after_confirmed_send"
        result = await db.execute(
            update(ReminderOccurrence)
            .where(
                ReminderOccurrence.id == occurrence.id,
                ReminderOccurrence.lease_token == token,
                ReminderOccurrence.state == occurrence.state,
                ReminderOccurrence.confirmed_count == occurrence.confirmed_count,
            )
            .values(**values)
        )
        await db.commit()
        return result.rowcount == 1


async def finalize_claim(
    claim: ReminderClaim,
    *,
    status: str | None = None,
    next_due_at: datetime | None = None,
    continuation_state: str | None = None,
    failure_reason: str = "",
    now: datetime | None = None,
) -> ReminderTask:
    if status is None:
        status = "active" if next_due_at is not None else "completed"
    if status not in {"active", "completed", "paused"}:
        raise ValueError("status must be active, completed, or paused")
    if status == "active" and next_due_at is None:
        raise ValueError("active finalization requires next_due_at")
    if status != "active" and next_due_at is not None:
        raise ValueError("only active finalization accepts next_due_at")
    next_due = _utc_naive(next_due_at, "next_due_at") if next_due_at is not None else None
    reason = _bounded(failure_reason, MAX_REASON, "failure_reason")
    continuation_state = (
        _bounded(continuation_state, MAX_CONTINUATION, "continuation_state") if continuation_state is not None else None
    )
    stamp = _now_naive(now)
    async with get_session() as db:
        task = await db.scalar(
            select(ReminderTask).where(
                ReminderTask.task_ref == claim.task_ref,
                ReminderTask.revision == claim.revision,
                ReminderTask.status == "active",
            )
        )
        occurrence = await db.scalar(
            select(ReminderOccurrence).where(
                ReminderOccurrence.task_ref == claim.task_ref,
                ReminderOccurrence.sequence == claim.occurrence_sequence,
                ReminderOccurrence.lease_token == claim.lease_token,
                ReminderOccurrence.state.in_(CLAIM_STATES),
            )
        )
        if task is None or occurrence is None:
            raise ReminderConflict("stale or cancelled reminder claim")
        values: dict[str, Any] = {
            "revision": task.revision + 1,
            "status": status,
            "updated_at": stamp,
        }
        if status == "active":
            values["due_at"] = next_due
            values["paused_reason"] = ""
        elif status == "paused":
            values["paused_reason"] = reason or "paused_after_occurrence"
        else:
            values["paused_reason"] = ""
        if continuation_state is not None:
            values["continuation_state"] = continuation_state
        result = await db.execute(
            update(ReminderTask)
            .where(ReminderTask.id == task.id, ReminderTask.revision == claim.revision, ReminderTask.status == "active")
            .values(**values)
        )
        if result.rowcount != 1:
            raise ReminderConflict("stale reminder claim")
        occurrence.state = "paused" if status == "paused" else "completed"
        occurrence.failure_reason = reason
        occurrence.finished_at = stamp
        occurrence.lease_token = None
        occurrence.lease_deadline = None
        await db.commit()
        found = await db.scalar(select(ReminderTask).where(ReminderTask.task_ref == claim.task_ref))
        if found is None:
            raise ReminderConflict("task disappeared after finalization")
        return found


async def get_mutation(operation_id: str) -> ReminderMutation | None:
    """Read a submitted mutation without replaying its write."""
    async with get_session() as db:
        return await _mutation(db, _operation_id(operation_id))


record_transport_started = record_external_started
record_confirmed_receipt = record_confirmed
finalize_occurrence = finalize_claim
