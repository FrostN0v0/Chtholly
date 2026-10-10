"""Transaction, authorization and strict decoding primitives for the companion store."""

from __future__ import annotations

import json
from typing import Any
from secrets import token_hex
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from sqlalchemy import text, select
from sqlalchemy.ext.asyncio import AsyncSession

from utils.companion_core import LifeState, AttentionUpdate, ContactProposal

from .types import (
    ReviewClaim,
    ContactClaim,
    ModeSnapshot,
    MemberSnapshot,
    CompanionConflict,
    CompanionCorruptState,
    freeze,
)
from .models import CompanionLife, CompanionMode, CompanionMember, CompanionReview, CompanionContact
from ..models import ChatScope, ReminderPreference, ScopePersonaSelection

UTC = timezone.utc
SHANGHAI = timezone(timedelta(hours=8))
ACTIVE = "active"
FINAL_STATUSES = frozenset({"delivered", "silent", "declined", "failed", "cancelled", "unknown"})


def naive(value: datetime, name: str = "now") -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC).replace(tzinfo=None)


def aware(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=UTC) if value is not None else None


def bounded(value: str, name: str, size: int = 200, *, required: bool = True) -> str:
    if not isinstance(value, str) or len(value) > size or (required and not value.strip()):
        raise ValueError(f"Invalid {name}")
    return value


def positive(value: int, name: str, *, maximum: int = 10000) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"Invalid {name}")
    return value


def token(prefix: str) -> str:
    return f"{prefix}_{token_hex(16)}"


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)


def decode(model: Any, raw: str, label: str) -> Any:
    try:
        return model.model_validate(json.loads(raw))
    except (ValueError, TypeError) as error:
        raise CompanionCorruptState(f"Invalid persisted {label}") from error


def life_state(row: CompanionLife | None) -> LifeState:
    if row is not None and row.version < 0:
        raise CompanionCorruptState("Invalid persisted LifeState version")
    return decode(LifeState, row.state_json, "LifeState") if row is not None else LifeState()


def attention_state(raw: str) -> AttentionUpdate:
    return decode(AttentionUpdate, raw, "attention")


def contact_proposal(raw: str) -> ContactProposal:
    return decode(ContactProposal, raw, "contact proposal")


def receipts(row: CompanionContact) -> tuple[str, ...]:
    try:
        values = json.loads(row.confirmed_receipts_json)
        if (
            not isinstance(values, list)
            or any(not isinstance(value, str) or not value.strip() or len(value) > 200 for value in values)
            or len(values) != len(set(values))
        ):
            raise ValueError("Invalid receipt list")
        return tuple(values)
    except (ValueError, TypeError) as error:
        raise CompanionCorruptState("Invalid persisted contact receipts") from error


@asynccontextmanager
async def session(session_factory: Any = None) -> AsyncIterator[AsyncSession]:
    if session_factory is None:
        from .store import get_session

        session_factory = get_session
    async with session_factory() as db:
        yield db


@asynccontextmanager
async def transaction(session_factory: Any = None) -> AsyncIterator[AsyncSession]:
    async with session(session_factory) as db:
        async with db.begin():
            # SQLite serializes the read/modify/write decision, including absent rows.
            # The transaction context always rolls back exceptions and failed commits.
            if db.get_bind().dialect.name == "sqlite":
                await db.execute(text("BEGIN IMMEDIATE"))
            yield db


def mode_snapshot(row: CompanionMode | None, scope_id: int, persona_key: str = "") -> ModeSnapshot:
    if row is None:
        return ModeSnapshot(scope_id, "normal", 0, persona_key, None, 0)
    if row.mode not in {"normal", "companion"} or row.revision < 0 or row.context_revision < 0:
        raise CompanionCorruptState("Invalid persisted conversation mode")
    return ModeSnapshot(
        row.scope_id, row.mode, row.revision, row.persona_key, aware(row.next_review_at), row.context_revision
    )


def member_snapshot(row: CompanionMember) -> MemberSnapshot:
    if row.version < 0 or row.last_turn_id < 0 or not row.user_id or not row.platform_user_id:
        raise CompanionCorruptState("Invalid persisted companion member")
    return MemberSnapshot(
        row.scope_id,
        row.persona_key,
        row.member_ref,
        row.user_id,
        row.platform_user_id,
        row.display_name,
        row.version,
        row.last_turn_id,
        aware(row.last_user_at),
        aware(row.last_contact_at),
    )


async def native_scope(db: AsyncSession, scope_id: int, persona_key: str | None = None) -> ChatScope:
    scope = await db.get(ChatScope, scope_id)
    if scope is None:
        raise CompanionConflict("Unknown chat scope")
    if persona_key is not None:
        selection = await db.get(ScopePersonaSelection, scope_id)
        if selection is None or selection.persona_key != persona_key:
            raise CompanionConflict("Companion persona does not match the native scope selection")
    return scope


async def enabled_mode(db: AsyncSession, scope_id: int, persona_key: str) -> CompanionMode | None:
    row = await db.get(CompanionMode, scope_id)
    if row is None or row.mode != "companion" or row.persona_key != persona_key:
        return None
    selection = await db.get(ScopePersonaSelection, scope_id)
    if selection is None or selection.persona_key != persona_key or await db.get(ChatScope, scope_id) is None:
        return None
    return row


async def permitted(db: AsyncSession, scope_id: int, user_id: str) -> bool:
    scope = await db.get(ChatScope, scope_id)
    if scope is None:
        return False
    preference = await db.get(ReminderPreference, (scope.account_id, user_id))
    return preference is None or preference.enabled


async def review_rows(
    db: AsyncSession, claim: ReviewClaim, now: datetime
) -> tuple[CompanionMode, CompanionReview] | None:
    mode = await enabled_mode(db, claim.scope_id, claim.persona_key)
    review = await db.get(CompanionReview, claim.lease_token)
    life = await db.get(CompanionLife, (claim.scope_id, claim.persona_key))
    if (
        mode is None
        or review is None
        or review.status != ACTIVE
        or mode.review_token != claim.lease_token
        or mode.review_deadline is None
        or mode.review_deadline <= now
        or mode.revision != claim.mode_revision
        or mode.context_revision != claim.context_revision
        or review.scope_id != claim.scope_id
        or review.persona_key != claim.persona_key
        or review.mode_revision != claim.mode_revision
        or review.context_revision != claim.context_revision
        or review.life_version != claim.life_version
        or (life.version if life else 0) != claim.life_version
    ):
        return None
    return mode, review


def bound_contact(row: CompanionContact | None, claim: ContactClaim) -> bool:
    return row is not None and (
        row.attempt_ref,
        row.scope_id,
        row.persona_key,
        row.mode_revision,
        row.context_revision,
        row.member_ref,
        row.member_version,
        row.user_id,
        row.platform_user_id,
        row.display_name,
        row.lease_token,
    ) == (
        claim.attempt_ref,
        claim.scope_id,
        claim.persona_key,
        claim.mode_revision,
        claim.context_revision,
        claim.member_ref,
        claim.member_version,
        claim.user_id,
        claim.platform_user_id,
        claim.display_name,
        claim.lease_token,
    )


async def valid_contact(db: AsyncSession, row: CompanionContact | None, claim: ContactClaim, now: datetime) -> bool:
    if not bound_contact(row, claim) or row.status != ACTIVE or row.lease_deadline <= now:
        return False
    mode = await enabled_mode(db, claim.scope_id, claim.persona_key)
    member = await db.get(CompanionMember, (claim.scope_id, claim.persona_key, claim.member_ref))
    return (
        mode is not None
        and mode.revision == claim.mode_revision
        and mode.context_revision == claim.context_revision
        and member is not None
        and member.version == claim.member_version
        and member.user_id == claim.user_id
        and member.platform_user_id == claim.platform_user_id
        and await permitted(db, claim.scope_id, claim.user_id)
    )


async def invalidate(db: AsyncSession, mode: CompanionMode, now: datetime, reason: str) -> None:
    if mode.review_token is not None:
        review = await db.get(CompanionReview, mode.review_token)
        if review is not None and review.status == ACTIVE:
            review.status, review.reason, review.finished_at = "cancelled", reason, now
    mode.review_token = mode.review_deadline = None
    rows = (
        await db.scalars(
            select(CompanionContact).where(
                CompanionContact.scope_id == mode.scope_id,
                CompanionContact.status == ACTIVE,
            )
        )
    ).all()
    for row in rows:
        confirmed = receipts(row)
        row.status = "unknown" if row.transport_inflight else ("delivered" if confirmed else "cancelled")
        row.reason, row.finished_at = reason, now
        row.lease_deadline = now
        if row.transport_started_at is not None:
            member = await db.get(CompanionMember, (row.scope_id, row.persona_key, row.member_ref))
            if member is not None:
                member.last_contact_at = max(
                    member.last_contact_at or row.transport_started_at, row.transport_started_at
                )
        if confirmed:
            from .claim_store import _waiting_reply

            await _waiting_reply(db, row, now)


def contact_snapshot(row: CompanionContact) -> dict:
    proposal = contact_proposal(row.proposal_json)
    return freeze(
        {
            "attempt_ref": row.attempt_ref,
            "scope_id": row.scope_id,
            "persona_key": row.persona_key,
            "member_ref": row.member_ref,
            "user_id": row.user_id,
            "platform_user_id": row.platform_user_id,
            "status": row.status,
            "reason": row.reason,
            "proposal": proposal.model_dump(mode="json"),
            "turn_id": row.turn_id,
            "transport_started_at": aware(row.transport_started_at),
            "transport_inflight": row.transport_inflight,
            "receipts": receipts(row),
            "created_at": aware(row.created_at),
            "finished_at": aware(row.finished_at),
        }
    )
