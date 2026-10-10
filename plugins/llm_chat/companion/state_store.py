"""Durable reflection reservations and revision-fenced generated-state commits."""

from __future__ import annotations

from typing import Any
from datetime import datetime, timedelta
from collections.abc import Sequence

from sqlalchemy import func, select

from utils.companion_core import LifePlan, LifeEvent, LifeState, ReflectionProposal

from .types import ReviewClaim, ModeSnapshot, CompanionConflict, ReflectionSnapshot, CompanionCorruptState, freeze
from .models import (
    CompanionLife,
    CompanionMode,
    CompanionMember,
    CompanionReview,
    CompanionContact,
    CompanionAttention,
    CompanionLifeRecord,
    CompanionChatEvidence,
)
from ..models import AgentTurn, ContextSession, ScopePersonaSelection
from .persistence import (
    ACTIVE,
    SHANGHAI,
    aware,
    dumps,
    naive,
    token,
    decode,
    bounded,
    session,
    positive,
    permitted,
    life_state,
    review_rows,
    transaction,
    enabled_mode,
    mode_snapshot,
    attention_state,
    contact_snapshot,
)


async def validate_evidence(
    db: Any,
    scope_id: int,
    persona_key: str,
    member_ref: str,
    turn_ids: Sequence[int],
    *,
    required: bool,
) -> CompanionMember:
    member = await db.get(CompanionMember, (scope_id, persona_key, member_ref))
    if member is None or not await permitted(db, scope_id, member.user_id):
        raise CompanionConflict("Proposal does not name an eligible registered member")
    if required and not turn_ids:
        raise CompanionConflict("Private attention and follow-up contacts require actual member evidence")
    if len(turn_ids) != len(set(turn_ids)):
        raise CompanionConflict("Duplicate contact or attention evidence")
    for turn_id in turn_ids:
        positive(turn_id, "evidence turn_id", maximum=2**63 - 1)
        evidence = await db.get(CompanionChatEvidence, (scope_id, persona_key, turn_id))
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
        if evidence is None or evidence.member_ref != member_ref or native is None:
            raise CompanionConflict("Evidence is not owned by the proposal's scoped member")
        turn, context = native
        if (
            context.scope_id != scope_id
            or context.purpose != "chat"
            or turn.trigger_kind != "message"
            or turn.status not in {"completed", "silent", "declined"}
            or turn.finished_at is None
            or turn.user_id not in {member.user_id, member.platform_user_id}
        ):
            raise CompanionConflict("Evidence is not a completed ordinary chat from this member")
    return member


async def preserve_life_continuity(
    db: Any, claim: ReviewClaim, previous: LifeState, proposed: LifeState, now: datetime
) -> None:
    new_plans = {plan.key: plan for plan in proposed.plans}
    for plan in previous.plans:
        if plan.status != "completed" and plan.key not in new_plans:
            raise CompanionConflict("An unfinished fictional plan cannot disappear")
        if plan.status == "completed" and plan.key in new_plans and new_plans[plan.key].status != "completed":
            raise CompanionConflict("A completed fictional plan cannot revert under the same key")
    new_events = {event.key: event for event in proposed.recent_events}
    for event in previous.recent_events:
        if event.key in new_events and event.model_dump(mode="json") != new_events[event.key].model_dump(mode="json"):
            raise CompanionConflict("A retained fictional event cannot be rewritten")
    for kind, items, model in (("plan", proposed.plans, LifePlan), ("event", proposed.recent_events, LifeEvent)):
        for item in items:
            key = (claim.scope_id, claim.persona_key, kind, item.key)
            recorded = await db.get(CompanionLifeRecord, key)
            if recorded is not None:
                old = decode(model, recorded.state_json, f"fictional {kind} record")
                if old.key != item.key:
                    raise CompanionCorruptState("Fictional life record does not match its stable key")
                if kind == "plan" and old.status == "completed" and item.status != "completed":
                    raise CompanionConflict("A completed fictional plan cannot revert under the same key")
                if kind == "event" and old.model_dump(mode="json") != item.model_dump(mode="json"):
                    raise CompanionConflict("A recorded fictional event cannot be rewritten")
                recorded.state_json, recorded.updated_at = dumps(item.model_dump(mode="json")), now
            else:
                db.add(
                    CompanionLifeRecord(
                        scope_id=claim.scope_id,
                        persona_key=claim.persona_key,
                        kind=kind,
                        key=item.key,
                        state_json=dumps(item.model_dump(mode="json")),
                        updated_at=now,
                    )
                )


async def due_scopes(*, now: datetime, limit: int = 20) -> tuple[ModeSnapshot, ...]:
    current_time = naive(now)
    positive(limit, "limit", maximum=100)
    async with session() as db:
        rows = (
            await db.scalars(
                select(CompanionMode)
                .join(
                    ScopePersonaSelection,
                    ScopePersonaSelection.scope_id == CompanionMode.scope_id,
                )
                .where(
                    CompanionMode.mode == "companion",
                    CompanionMode.persona_key == ScopePersonaSelection.persona_key,
                    CompanionMode.next_review_at <= current_time,
                    (CompanionMode.review_token.is_(None)) | (CompanionMode.review_deadline <= current_time),
                )
                .order_by(CompanionMode.next_review_at, CompanionMode.scope_id)
                .limit(limit)
            )
        ).all()
        return tuple(mode_snapshot(row, row.scope_id) for row in rows)


async def claim_review(
    scope_id: int,
    persona_key: str,
    *,
    now: datetime,
    lease_seconds: int = 180,
    daily_limit: int = 12,
) -> ReviewClaim | None:
    current_time = naive(now)
    positive(lease_seconds, "lease_seconds", maximum=3600)
    positive(daily_limit, "daily_limit", maximum=48)
    budget_day = now.astimezone(SHANGHAI).date().isoformat()
    async with transaction() as db:
        mode = await enabled_mode(db, scope_id, persona_key)
        if mode is None or mode.next_review_at is None or mode.next_review_at > current_time:
            return None
        if mode.review_token is not None:
            if mode.review_deadline is None:
                raise CompanionCorruptState("Reflection lease is missing its deadline")
            if mode.review_deadline > current_time:
                return None
            old = await db.get(CompanionReview, mode.review_token)
            if old is not None and old.status == ACTIVE:
                old.status, old.reason, old.finished_at = "expired", "lease_expired", current_time
            mode.review_token = mode.review_deadline = None
        used = await db.scalar(
            select(func.count())
            .select_from(CompanionReview)
            .where(
                CompanionReview.scope_id == scope_id,
                CompanionReview.budget_day == budget_day,
            )
        )
        if used >= daily_limit:
            next_day = now.astimezone(SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            mode.next_review_at = naive(next_day)
            return None
        life = await db.get(CompanionLife, (scope_id, persona_key))
        life_state(life)
        lease_token = token("review")
        deadline = current_time + timedelta(seconds=lease_seconds)
        version = life.version if life else 0
        review = CompanionReview(
            lease_token=lease_token,
            scope_id=scope_id,
            persona_key=persona_key,
            mode_revision=mode.revision,
            context_revision=mode.context_revision,
            life_version=version,
            lease_deadline=deadline,
            budget_day=budget_day,
            status=ACTIVE,
            started_at=current_time,
        )
        db.add(review)
        mode.review_token, mode.review_deadline = lease_token, deadline
        return ReviewClaim(
            scope_id, persona_key, mode.revision, mode.context_revision, lease_token, aware(deadline), version
        )


async def load_reflection(claim: ReviewClaim, *, now: datetime, member_idle_days: int = 14) -> ReflectionSnapshot:
    current_time = naive(now)
    positive(member_idle_days, "member_idle_days", maximum=90)
    async with session() as db:
        rows = await review_rows(db, claim, current_time)
        if rows is None:
            raise CompanionConflict("Reflection capability is no longer current")
        mode, _ = rows
        from .store import active_members

        members = await active_members(db, claim.scope_id, claim.persona_key, current_time, member_idle_days)
        member_refs = {member.member_ref for member in members}
        attention_rows = (
            await db.scalars(
                select(CompanionAttention)
                .where(
                    CompanionAttention.scope_id == claim.scope_id,
                    CompanionAttention.persona_key == claim.persona_key,
                    CompanionAttention.member_ref.in_(member_refs),
                )
                .order_by(CompanionAttention.updated_at.desc(), CompanionAttention.member_ref, CompanionAttention.key)
                .limit(100)
            )
        ).all()
        attention = []
        historical_ids: dict[str, set[int]] = {ref: set() for ref in member_refs}
        for row in attention_rows:
            item = attention_state(row.state_json)
            if item.member_ref != row.member_ref or item.key != row.key:
                raise CompanionCorruptState("Persisted attention is not bound to its indexed owner")
            await validate_evidence(
                db, claim.scope_id, claim.persona_key, item.member_ref, item.evidence_turn_ids, required=True
            )
            historical_ids[item.member_ref].update(item.evidence_turn_ids)
            attention.append(freeze(item.model_dump(mode="json")))
        evidence_ids = {}
        for member in members:
            ids = (
                await db.scalars(
                    select(CompanionChatEvidence.turn_id)
                    .where(
                        CompanionChatEvidence.scope_id == claim.scope_id,
                        CompanionChatEvidence.persona_key == claim.persona_key,
                        CompanionChatEvidence.member_ref == member.member_ref,
                    )
                    .order_by(CompanionChatEvidence.turn_id.desc())
                    .limit(32)
                )
            ).all()
            all_ids = sorted(set(ids) | historical_ids[member.member_ref])
            await validate_evidence(db, claim.scope_id, claim.persona_key, member.member_ref, all_ids, required=False)
            evidence_ids[member.member_ref] = tuple(all_ids)
        contact_rows = (
            await db.scalars(
                select(CompanionContact)
                .where(
                    CompanionContact.scope_id == claim.scope_id,
                    CompanionContact.persona_key == claim.persona_key,
                    CompanionContact.member_ref.in_(member_refs),
                )
                .order_by(CompanionContact.created_at.desc(), CompanionContact.attempt_ref)
                .limit(20)
            )
        ).all()
        life = await db.get(CompanionLife, (claim.scope_id, claim.persona_key))
        return ReflectionSnapshot(
            mode_snapshot(mode, mode.scope_id),
            life_state(life),
            life.version if life else 0,
            members,
            tuple(attention),
            tuple(contact_snapshot(row) for row in contact_rows),
            freeze(evidence_ids),
        )


async def finish_review(
    claim: ReviewClaim,
    proposal: ReflectionProposal,
    *,
    now: datetime,
    next_review_at: datetime,
) -> bool:
    current_time = naive(now)
    desired_due = naive(next_review_at, "next_review_at")
    desired_due = max(current_time + timedelta(minutes=10), min(desired_due, current_time + timedelta(hours=24)))
    # Revalidate nested containers rather than trusting a frozen model's mutable lists.
    validated = ReflectionProposal.model_validate(proposal.model_dump(mode="json"))
    if len({item.key for item in validated.life.plans}) != len(validated.life.plans):
        raise ValueError("Duplicate life plan keys")
    if len({item.key for item in validated.life.recent_events}) != len(validated.life.recent_events):
        raise ValueError("Duplicate life event keys")
    keys = {(item.member_ref, item.key) for item in validated.attention}
    if len(keys) != len(validated.attention):
        raise ValueError("Duplicate attention keys")
    async with transaction() as db:
        rows = await review_rows(db, claim, current_time)
        if rows is None:
            return False
        mode, review = rows
        for item in validated.attention:
            await validate_evidence(
                db, claim.scope_id, claim.persona_key, item.member_ref, item.evidence_turn_ids, required=True
            )
        if validated.contact is not None:
            item = validated.contact
            await validate_evidence(
                db,
                claim.scope_id,
                claim.persona_key,
                item.member_ref,
                item.evidence_turn_ids,
                required=item.purpose in {"check_in", "continue"},
            )
        life = await db.get(CompanionLife, (claim.scope_id, claim.persona_key))
        encoded = dumps(validated.life.model_dump(mode="json"))
        previous_life = life_state(life)
        await preserve_life_continuity(db, claim, previous_life, validated.life, current_time)
        old_encoded = dumps(previous_life.model_dump(mode="json"))
        if life is None:
            life = CompanionLife(
                scope_id=claim.scope_id,
                persona_key=claim.persona_key,
                state_json=encoded,
                version=1 if encoded != old_encoded else 0,
                updated_at=current_time,
            )
            db.add(life)
        elif encoded != old_encoded:
            life.state_json, life.updated_at = encoded, current_time
            life.version += 1
        for item in validated.attention:
            key = (claim.scope_id, claim.persona_key, item.member_ref, item.key)
            attention = await db.get(CompanionAttention, key)
            encoded_attention = dumps(item.model_dump(mode="json"))
            if attention is None:
                db.add(
                    CompanionAttention(
                        scope_id=claim.scope_id,
                        persona_key=claim.persona_key,
                        member_ref=item.member_ref,
                        key=item.key,
                        state_json=encoded_attention,
                        version=1,
                        updated_at=current_time,
                    )
                )
            else:
                prior = attention_state(attention.state_json)
                if prior.member_ref != attention.member_ref or prior.key != attention.key:
                    raise CompanionCorruptState("Persisted attention is not bound to its indexed owner")
                if dumps(prior.model_dump(mode="json")) != encoded_attention:
                    attention.state_json, attention.updated_at = encoded_attention, current_time
                    attention.version += 1
        review.committed_life_version = life.version
        review.contact_proposal_json = (
            dumps(validated.contact.model_dump(mode="json")) if validated.contact is not None else None
        )
        review.status, review.finished_at = "completed", current_time
        mode.review_token = mode.review_deadline = None
        mode.next_review_at, mode.updated_at = desired_due, current_time
        return True


async def fail_review(claim: ReviewClaim, *, now: datetime, retry_at: datetime, reason: str) -> bool:
    current_time = naive(now)
    retry = max(
        current_time + timedelta(minutes=1), min(naive(retry_at, "retry_at"), current_time + timedelta(hours=24))
    )
    bounded(reason, "reason", 1000, required=False)
    async with transaction() as db:
        mode = await enabled_mode(db, claim.scope_id, claim.persona_key)
        review = await db.get(CompanionReview, claim.lease_token)
        if (
            mode is None
            or review is None
            or review.status != ACTIVE
            or mode.review_token != claim.lease_token
            or mode.revision != claim.mode_revision
            or mode.context_revision != claim.context_revision
        ):
            return False
        review.status, review.reason, review.finished_at = "failed", reason, current_time
        mode.review_token = mode.review_deadline = None
        mode.next_review_at, mode.updated_at = retry, current_time
        return True
