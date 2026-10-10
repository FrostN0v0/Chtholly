"""Mode, verified-member feedback, initialization and ordinary-context persistence.

Every write uses an explicit rollback transaction. Normal-mode reads never create
rows, and these functions never call a model or transport.
"""

from __future__ import annotations

from typing import Any
from datetime import datetime, timedelta

from sqlalchemy import select
from entari_plugin_database import Base, get_session

from .types import ModeSnapshot, MemberSnapshot, CompanionConflict
from .models import (
    OWNED_TABLES,
    CompanionLife,
    CompanionMode,
    CompanionMember,
    CompanionReview,
    CompanionContact,
    CompanionAttention,
    CompanionChatEvidence,
)
from ..models import AgentTurn, ContextSession, ScopePersonaSelection
from .persistence import (
    UTC,
    ACTIVE,
    naive,
    token,
    bounded,
    session,
    positive,
    receipts,
    permitted,
    invalidate,
    life_state,
    transaction,
    enabled_mode,
    native_scope,
    mode_snapshot,
    attention_state,
    member_snapshot,
    contact_snapshot,
)

__all__ = [
    "bind_contact_turn",
    "claim_contact",
    "claim_review",
    "contact_valid",
    "due_scopes",
    "fail_review",
    "finish_contact",
    "finish_review",
    "get_mode",
    "get_session",
    "initialize_store",
    "load_reflection",
    "read_companion_context",
    "read_contact_history",
    "read_members",
    "rebind_persona",
    "record_chat",
    "record_receipts",
    "record_transport_started",
    "recover_stale",
    "register_member",
    "renew_contact",
    "set_mode",
]


async def initialize_store(*, now: datetime | None = None) -> None:
    """Create only the additive owned tables, then recover expired capabilities."""
    async with transaction() as db:
        connection = await db.connection()
        await connection.run_sync(
            lambda conn: Base.metadata.create_all(conn, tables=list(OWNED_TABLES), checkfirst=True)
        )
    await recover_stale(now=now or datetime.now(UTC))


async def get_mode(scope_id: int, *, session_factory: Any = None) -> ModeSnapshot:
    async with session(session_factory) as db:
        row = await db.get(CompanionMode, scope_id)
        selection = await db.get(ScopePersonaSelection, scope_id)
        return mode_snapshot(row, scope_id, selection.persona_key if selection else "")


async def set_mode(
    scope_id: int,
    mode: str,
    *,
    persona_key: str,
    now: datetime,
    expected_revision: int | None = None,
) -> ModeSnapshot:
    if mode not in {"normal", "companion"}:
        raise ValueError("Invalid conversation mode")
    bounded(persona_key, "persona_key")
    current_time = naive(now)
    async with transaction() as db:
        await native_scope(db, scope_id, persona_key)
        row = await db.get(CompanionMode, scope_id)
        revision = row.revision if row else 0
        if expected_revision is not None and (isinstance(expected_revision, bool) or expected_revision != revision):
            raise CompanionConflict("Conversation mode revision changed")
        if row is None:
            if mode == "normal":
                return mode_snapshot(None, scope_id, persona_key)
            row = CompanionMode(
                scope_id=scope_id,
                mode=mode,
                revision=1,
                persona_key=persona_key,
                context_revision=0,
                next_review_at=current_time,
                updated_at=current_time,
            )
            db.add(row)
        elif row.mode != mode or row.persona_key != persona_key:
            await invalidate(db, row, current_time, "mode_changed" if row.mode != mode else "persona_changed")
            row.mode, row.persona_key = mode, persona_key
            row.revision += 1
            row.context_revision += 1
            row.next_review_at = current_time if mode == "companion" else None
            row.updated_at = current_time
        return mode_snapshot(row, scope_id)


async def rebind_persona(scope_id: int, persona_key: str, *, now: datetime) -> ModeSnapshot:
    bounded(persona_key, "persona_key")
    current_time = naive(now)
    async with transaction() as db:
        await native_scope(db, scope_id, persona_key)
        row = await db.get(CompanionMode, scope_id)
        if row is None:
            return mode_snapshot(None, scope_id, persona_key)
        if row.persona_key != persona_key:
            await invalidate(db, row, current_time, "persona_changed")
            row.persona_key = persona_key
            row.revision += 1
            row.context_revision += 1
            row.next_review_at = current_time if row.mode == "companion" else None
            row.updated_at = current_time
        return mode_snapshot(row, scope_id)


async def _member(
    db: Any,
    scope_id: int,
    persona_key: str,
    *,
    user_id: str,
    platform_user_id: str,
    display_name: str,
    now: datetime,
) -> tuple[CompanionMember, bool]:
    row = await db.scalar(
        select(CompanionMember).where(
            CompanionMember.scope_id == scope_id,
            CompanionMember.persona_key == persona_key,
            CompanionMember.user_id == user_id,
        )
    )
    if row is not None:
        if row.platform_user_id != platform_user_id:
            raise CompanionConflict("Verified member has a different immutable platform target")
        return row, False
    other = await db.scalar(
        select(CompanionMember.member_ref).where(
            CompanionMember.scope_id == scope_id,
            CompanionMember.persona_key == persona_key,
            CompanionMember.platform_user_id == platform_user_id,
        )
    )
    if other is not None:
        raise CompanionConflict("Platform target is already bound to another verified member")
    row = CompanionMember(
        scope_id=scope_id,
        persona_key=persona_key,
        member_ref=token("member"),
        user_id=user_id,
        platform_user_id=platform_user_id,
        display_name=display_name,
        version=0,
        last_turn_id=0,
        last_user_at=now,
    )
    db.add(row)
    return row, True


def _identity(user_id: str, platform_user_id: str, display_name: str) -> None:
    bounded(user_id, "user_id")
    bounded(platform_user_id, "platform_user_id")
    bounded(display_name, "display_name", required=False)


async def register_member(
    scope_id: int,
    persona_key: str,
    *,
    user_id: str,
    platform_user_id: str,
    display_name: str,
    now: datetime,
    turn_id: int = 0,
) -> MemberSnapshot | None:
    """Register a host-verified command actor without manufacturing completed evidence."""
    if isinstance(turn_id, bool) or turn_id != 0:
        raise ValueError("register_member does not create completed-turn evidence")
    _identity(user_id, platform_user_id, display_name)
    current_time = naive(now)
    async with transaction() as db:
        mode = await enabled_mode(db, scope_id, persona_key)
        if mode is None:
            return None
        member, created = await _member(
            db,
            scope_id,
            persona_key,
            user_id=user_id,
            platform_user_id=platform_user_id,
            display_name=display_name,
            now=current_time,
        )
        if created:
            await invalidate(db, mode, current_time, "verified_member_registered")
            mode.context_revision += 1
            mode.next_review_at = current_time
            mode.updated_at = current_time
        return member_snapshot(member)


async def record_chat(
    scope_id: int,
    persona_key: str,
    *,
    user_id: str,
    platform_user_id: str,
    display_name: str,
    turn_id: int,
    now: datetime,
    expected_revision: int | None = None,
) -> MemberSnapshot | None:
    """Record idempotent real-chat evidence, without inferring reply sentiment."""
    _identity(user_id, platform_user_id, display_name)
    positive(turn_id, "turn_id", maximum=2**63 - 1)
    current_time = naive(now)
    async with transaction() as db:
        mode = await enabled_mode(db, scope_id, persona_key)
        if mode is None:
            return None
        if expected_revision is not None:
            if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
                raise ValueError("Expected companion revision must be an integer")
            if mode.revision != expected_revision:
                return None
        native = (
            await db.execute(
                select(AgentTurn, ContextSession)
                .join(
                    ContextSession,
                    ContextSession.id == AgentTurn.session_id,
                )
                .where(AgentTurn.id == turn_id)
            )
        ).one_or_none()
        if native is None:
            raise CompanionConflict("Completed chat turn does not exist")
        turn, context = native
        if (
            context.scope_id != scope_id
            or context.purpose != "chat"
            or turn.trigger_kind != "message"
            or turn.status not in {"completed", "silent", "declined"}
            or turn.finished_at is None
            or turn.user_id not in {user_id, platform_user_id}
        ):
            raise CompanionConflict("Turn is not an attributable completed ordinary chat")
        member, _ = await _member(
            db,
            scope_id,
            persona_key,
            user_id=user_id,
            platform_user_id=platform_user_id,
            display_name=display_name,
            now=current_time,
        )
        evidence = await db.get(CompanionChatEvidence, (scope_id, persona_key, turn_id))
        if evidence is not None:
            if evidence.member_ref != member.member_ref:
                raise CompanionConflict("Completed chat evidence belongs to a different member")
            return member_snapshot(member)
        db.add(
            CompanionChatEvidence(
                scope_id=scope_id,
                persona_key=persona_key,
                turn_id=turn_id,
                member_ref=member.member_ref,
                received_at=current_time,
            )
        )
        member.version += 1
        member.last_turn_id = max(member.last_turn_id, turn_id)
        member.last_user_at = max(member.last_user_at, current_time)
        member.display_name = display_name
        await invalidate(db, mode, current_time, "real_chat_feedback")
        mode.context_revision += 1
        mode.updated_at = current_time
        # Debounce ordinary responses without consuming a model request here.
        proposed_due = current_time + timedelta(minutes=1)
        mode.next_review_at = min(mode.next_review_at, proposed_due) if mode.next_review_at else proposed_due
        return member_snapshot(member)


async def read_members(
    scope_id: int,
    persona_key: str,
    *,
    now: datetime,
    member_idle_days: int = 14,
) -> tuple[MemberSnapshot, ...]:
    positive(member_idle_days, "member_idle_days", maximum=90)
    current_time = naive(now)
    async with session() as db:
        if await enabled_mode(db, scope_id, persona_key) is None:
            return ()
        return await active_members(db, scope_id, persona_key, current_time, member_idle_days)


async def active_members(
    db: Any, scope_id: int, persona_key: str, now: datetime, idle_days: int
) -> tuple[MemberSnapshot, ...]:
    rows = (
        await db.scalars(
            select(CompanionMember)
            .where(
                CompanionMember.scope_id == scope_id,
                CompanionMember.persona_key == persona_key,
                CompanionMember.last_user_at >= now - timedelta(days=idle_days),
                CompanionMember.last_user_at <= now,
            )
            .order_by(CompanionMember.last_user_at.desc(), CompanionMember.member_ref)
            .limit(100)
        )
    ).all()
    result = []
    for member in rows:
        if await permitted(db, scope_id, member.user_id):
            result.append(member_snapshot(member))
    return tuple(result)


async def read_companion_context(
    scope_id: int,
    persona_key: str,
    user_id: str,
    *,
    session_factory: Any = None,
) -> dict | None:
    async with session(session_factory) as db:
        mode = await enabled_mode(db, scope_id, persona_key)
        if mode is None:
            return None
        life = await db.get(CompanionLife, (scope_id, persona_key))
        member = await db.scalar(
            select(CompanionMember).where(
                CompanionMember.scope_id == scope_id,
                CompanionMember.persona_key == persona_key,
                CompanionMember.user_id == user_id,
            )
        )
        own_attention = []
        if member is not None:
            rows = (
                await db.scalars(
                    select(CompanionAttention)
                    .where(
                        CompanionAttention.scope_id == scope_id,
                        CompanionAttention.persona_key == persona_key,
                        CompanionAttention.member_ref == member.member_ref,
                    )
                    .order_by(CompanionAttention.updated_at.desc(), CompanionAttention.key)
                    .limit(24)
                )
            ).all()
            for row in rows:
                item = attention_state(row.state_json)
                if item.member_ref != member.member_ref or item.key != row.key:
                    from .types import CompanionCorruptState

                    raise CompanionCorruptState("Persisted attention does not match its owner")
                own_attention.append(item.model_dump(mode="json"))
        return {"mode": "companion", "life": life_state(life).model_dump(mode="json"), "attention": own_attention}


async def read_contact_history(
    scope_id: int,
    persona_key: str,
    *,
    member_ref: str | None = None,
    limit: int = 20,
) -> tuple[dict, ...]:
    positive(limit, "limit", maximum=100)
    async with session() as db:
        query = select(CompanionContact).where(
            CompanionContact.scope_id == scope_id,
            CompanionContact.persona_key == persona_key,
        )
        if member_ref is not None:
            query = query.where(CompanionContact.member_ref == member_ref)
        rows = (
            await db.scalars(
                query.order_by(CompanionContact.created_at.desc(), CompanionContact.attempt_ref).limit(limit)
            )
        ).all()
        return tuple(contact_snapshot(row) for row in rows)


async def recover_stale(*, now: datetime) -> int:
    """Settle expired leases without retrying a model, contact, or external action."""
    current_time = naive(now)
    recovered = 0
    async with transaction() as db:
        modes = (
            await db.scalars(
                select(CompanionMode).where(
                    CompanionMode.review_token.is_not(None),
                    CompanionMode.review_deadline <= current_time,
                )
            )
        ).all()
        for mode in modes:
            review = await db.get(CompanionReview, mode.review_token)
            if review is not None and review.status == ACTIVE:
                review.status, review.reason, review.finished_at = "expired", "lease_expired", current_time
            mode.review_token = mode.review_deadline = None
            mode.next_review_at = current_time if mode.mode == "companion" else None
            recovered += 1
        attempts = (
            await db.scalars(
                select(CompanionContact).where(
                    CompanionContact.status == ACTIVE,
                    CompanionContact.lease_deadline <= current_time,
                )
            )
        ).all()
        for row in attempts:
            row.status = "unknown" if row.transport_inflight else ("delivered" if receipts(row) else "cancelled")
            row.reason, row.finished_at = "lease_expired", current_time
            if row.transport_started_at is not None:
                member = await db.get(CompanionMember, (row.scope_id, row.persona_key, row.member_ref))
                if member is not None:
                    member.last_contact_at = max(
                        member.last_contact_at or row.transport_started_at, row.transport_started_at
                    )
            if receipts(row):
                from .claim_store import _waiting_reply

                await _waiting_reply(db, row, current_time)
            recovered += 1
    return recovered


from .claim_store import (
    claim_contact,
    contact_valid,
    renew_contact,
    finish_contact,
    record_receipts,
    bind_contact_turn,
    record_transport_started,
)
from .state_store import due_scopes, fail_review, claim_review, finish_review, load_reflection
