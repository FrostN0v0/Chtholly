"""Bounded native-contact reservations with irreversible transport audit boundaries."""

from __future__ import annotations

import json
from typing import Any
from hashlib import sha256
from datetime import datetime, timedelta
from dataclasses import asdict
from collections.abc import Sequence

from sqlalchemy import func, select

from utils.companion_core import CompanionLimits, ContactProposal, contact_ready, normalize_limits
from utils.companion_core.policy import contact_block_reason

from .types import ReviewClaim, ContactClaim, CompanionConflict, CompanionCorruptState
from .models import CompanionLife, CompanionMode, CompanionMember, CompanionReview, CompanionContact, CompanionAttention
from ..models import AgentTurn, ContextSession
from .persistence import (
    ACTIVE,
    SHANGHAI,
    FINAL_STATUSES,
    aware,
    dumps,
    naive,
    token,
    bounded,
    session,
    positive,
    receipts,
    life_state,
    transaction,
    enabled_mode,
    bound_contact,
    valid_contact,
    attention_state,
    contact_proposal,
)
from .state_store import validate_evidence
from .decision_audit import ContactDecision


def _claim(row: CompanionContact) -> ContactClaim:
    return ContactClaim(
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
        aware(row.lease_deadline),
    )


def _limits(row: CompanionContact) -> CompanionLimits:
    try:
        return normalize_limits(json.loads(row.limits_json))
    except (ValueError, TypeError) as error:
        raise CompanionCorruptState("Invalid persisted companion contact limits") from error


async def _budget(db: Any, scope_id: int, user_id: str, day: str, *, excluding: str | None = None) -> int:
    query = (
        select(func.count())
        .select_from(CompanionContact)
        .where(
            CompanionContact.scope_id == scope_id,
            CompanionContact.budget_day == day,
            CompanionContact.user_id == user_id,
            (CompanionContact.status == ACTIVE) | CompanionContact.transport_started_at.is_not(None),
        )
    )
    if excluding is not None:
        query = query.where(CompanionContact.attempt_ref != excluding)
    return await db.scalar(query)


async def _user_history(
    db: Any,
    member: CompanionMember,
    *,
    excluding: str | None = None,
) -> tuple[datetime | None, datetime]:
    latest_user = await db.scalar(
        select(func.max(CompanionMember.last_user_at)).where(
            CompanionMember.scope_id == member.scope_id,
            CompanionMember.user_id == member.user_id,
        )
    )
    last_user = max(member.last_user_at, latest_user or member.last_user_at)
    query = select(func.max(CompanionContact.transport_started_at)).where(
        CompanionContact.scope_id == member.scope_id,
        CompanionContact.user_id == member.user_id,
        CompanionContact.transport_started_at.is_not(None),
    )
    if excluding is not None:
        query = query.where(CompanionContact.attempt_ref != excluding)
    last_contact = await db.scalar(query)
    return last_contact, last_user


async def claim_contact(
    scope_id: int,
    persona_key: str,
    member_ref: str,
    proposal: ContactProposal,
    *,
    now: datetime,
    origin: ReviewClaim,
    limits: CompanionLimits,
    lease_seconds: int = 300,
    decision: ContactDecision | None = None,
) -> ContactClaim | None:
    current_time = naive(now)
    if decision is not None:
        decision.set("failed", "contact_claim_failed", at=now)
    positive(lease_seconds, "lease_seconds", maximum=3600)
    validated = ContactProposal.model_validate(proposal.model_dump(mode="json"))
    validated_limits = normalize_limits(asdict(limits))
    if validated.member_ref != member_ref:
        raise CompanionConflict("Contact proposal does not match the requested registered member")
    day = now.astimezone(SHANGHAI).date().isoformat()
    async with transaction() as db:
        mode = await enabled_mode(db, scope_id, persona_key)
        if mode is None:
            if decision is not None:
                decision.set("blocked", "mode_changed")
            return None
        if (origin.scope_id, origin.persona_key, origin.mode_revision, origin.context_revision) != (
            scope_id,
            persona_key,
            mode.revision,
            mode.context_revision,
        ):
            if decision is not None:
                decision.set("blocked", "mode_changed")
            return None
        review = await db.get(CompanionReview, origin.lease_token)
        life = await db.get(CompanionLife, (scope_id, persona_key))
        life_version = life.version if life is not None else 0
        if (
            review is None
            or (
                review.scope_id,
                review.persona_key,
                review.mode_revision,
                review.context_revision,
                review.life_version,
            )
            != (
                origin.scope_id,
                origin.persona_key,
                origin.mode_revision,
                origin.context_revision,
                origin.life_version,
            )
            or review.status != "completed"
            or review.committed_life_version != life_version
        ):
            if decision is not None:
                decision.set("blocked", "review_changed")
            return None
        if review.contact_proposal_json != dumps(validated.model_dump(mode="json")):
            raise CompanionConflict("Contact motive does not match its committed originating review")
        member = await validate_evidence(
            db,
            scope_id,
            persona_key,
            member_ref,
            validated.evidence_turn_ids,
            required=validated.purpose in {"check_in", "continue"},
        )
        blocked = await db.scalar(
            select(CompanionContact.attempt_ref)
            .where(
                CompanionContact.scope_id == scope_id,
                CompanionContact.persona_key == persona_key,
                CompanionContact.member_ref == member_ref,
                (
                    (CompanionContact.status == ACTIVE)
                    | ((CompanionContact.status == "unknown") & (CompanionContact.proposal_key == validated.key))
                ),
            )
            .limit(1)
        )
        if blocked is not None:
            if decision is not None:
                decision.set("blocked", "contact_inflight_or_unknown")
            return None
        count = await _budget(db, scope_id, member.user_id, day)
        last_contact, last_user = await _user_history(db, member)
        user_time = aware(last_user)
        contact_time = aware(last_contact)
        assert user_time is not None
        reason = contact_block_reason(
            now,
            last_contact=contact_time,
            last_user_at=user_time,
            contacts_today=count,
            limits=validated_limits,
        )
        if reason is not None:
            if decision is not None:
                decision.set(
                    "blocked",
                    reason,
                    facts={
                        "contacts_today": count,
                        "limits": asdict(validated_limits),
                        "last_contact_at": contact_time.isoformat() if contact_time else None,
                        "last_user_at": user_time.isoformat(),
                        "timezone": "Asia/Shanghai",
                    },
                )
            return None
        base = {"key": validated.key, "evidence": sorted(validated.evidence_turn_ids)}
        if validated.purpose == "free_chat":
            life = await db.get(CompanionLife, (scope_id, persona_key))
            life_state(life)
            base["life_version"] = life.version if life else 0
            base["member_version"] = member.version
        identity = sha256(dumps(base).encode("utf-8")).hexdigest()
        prior = await db.scalar(
            select(CompanionContact.attempt_ref).where(
                CompanionContact.scope_id == scope_id,
                CompanionContact.persona_key == persona_key,
                CompanionContact.member_ref == member_ref,
                CompanionContact.identity == identity,
            )
        )
        if prior is not None:
            if decision is not None:
                decision.set("blocked", "duplicate_contact")
            return None
        row = CompanionContact(
            attempt_ref=token("contact"),
            scope_id=scope_id,
            persona_key=persona_key,
            mode_revision=mode.revision,
            context_revision=mode.context_revision,
            member_ref=member_ref,
            origin_review_token=origin.lease_token,
            member_version=member.version,
            user_id=member.user_id,
            platform_user_id=member.platform_user_id,
            display_name=member.display_name,
            proposal_key=validated.key,
            identity=identity,
            proposal_json=dumps(validated.model_dump(mode="json")),
            limits_json=dumps(asdict(validated_limits)),
            previous_contact_at=last_contact,
            budget_day=day,
            lease_token=token("contact_lease"),
            lease_deadline=current_time + timedelta(seconds=lease_seconds),
            created_at=current_time,
            status=ACTIVE,
        )
        db.add(row)
        claim = _claim(row)
    if decision is not None:
        decision.set(
            "reserved", "contact_reserved", facts={"contacts_today": count, "limits": asdict(validated_limits)}
        )
        decision.attempt_ref = claim.attempt_ref
    return claim


async def contact_valid(claim: ContactClaim, *, now: datetime) -> bool:
    async with session() as db:
        row = await db.get(CompanionContact, claim.attempt_ref)
        return await valid_contact(db, row, claim, naive(now))


async def renew_contact(claim: ContactClaim, *, now: datetime, lease_seconds: int = 300) -> bool:
    current_time = naive(now)
    positive(lease_seconds, "lease_seconds", maximum=3600)
    async with transaction() as db:
        row = await db.get(CompanionContact, claim.attempt_ref)
        if not await valid_contact(db, row, claim, current_time):
            return False
        row.lease_deadline = current_time + timedelta(seconds=lease_seconds)
        return True


async def bind_contact_turn(claim: ContactClaim, turn_id: int, *, now: datetime | None = None) -> bool:
    positive(turn_id, "turn_id", maximum=2**63 - 1)
    async with transaction() as db:
        row = await db.get(CompanionContact, claim.attempt_ref)
        if not await valid_contact(db, row, claim, naive(now or datetime.now().astimezone())):
            return False
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
            raise CompanionConflict("Contact internal turn does not exist")
        turn, context = native
        if (
            context.scope_id != claim.scope_id
            or context.purpose != "companion"
            or turn.trigger_kind != "autonomous"
            or turn.user_id != claim.user_id
        ):
            raise CompanionConflict("Internal turn is not bound to the reserved native contact target")
        if row.turn_id is not None and row.turn_id != turn_id:
            raise CompanionConflict("Contact is already bound to a different internal turn")
        row.turn_id = turn_id
        return True


async def record_transport_started(claim: ContactClaim, *, now: datetime) -> bool:
    current_time = naive(now)
    day = now.astimezone(SHANGHAI).date().isoformat()
    async with transaction() as db:
        row = await db.get(CompanionContact, claim.attempt_ref)
        if not await valid_contact(db, row, claim, current_time) or row.transport_inflight:
            return False
        if row.turn_id is None:
            raise CompanionConflict("A contact must be bound to its real internal turn before transport")
        member = await db.get(CompanionMember, (claim.scope_id, claim.persona_key, claim.member_ref))
        limits = _limits(row)
        used = await _budget(db, claim.scope_id, claim.user_id, day, excluding=row.attempt_ref)
        last_contact, last_user = await _user_history(db, member, excluding=row.attempt_ref)
        if not contact_ready(
            now,
            last_contact=aware(last_contact),
            last_user_at=aware(last_user),
            contacts_today=used,
            limits=limits,
        ):
            return False
        # Commit this boundary before the caller attempts each native send.
        row.transport_inflight = True
        if row.transport_started_at is None:
            row.transport_started_at = current_time
            row.budget_day = day
        member.last_contact_at = max(member.last_contact_at or current_time, current_time)
        return True


async def record_receipts(claim: ContactClaim, receipts: Sequence[str], *, now: datetime) -> None:
    current_time = naive(now)
    if isinstance(receipts, (str, bytes)) or not isinstance(receipts, Sequence):
        raise ValueError("receipts must be an ordered sequence of real message IDs")
    incoming = tuple(receipts)
    for receipt in incoming:
        bounded(receipt, "receipt", 200)
    if not incoming:
        return
    from .persistence import receipts as confirmed_receipts

    async with transaction() as db:
        row = await db.get(CompanionContact, claim.attempt_ref)
        # Once a transport boundary exists, real receipts survive off, feedback,
        # lease expiry and persona changes. They never reauthorize another send.
        if not bound_contact(row, claim) or row.transport_started_at is None:
            raise CompanionConflict("Receipt does not belong to a recorded transport boundary")
        previous = confirmed_receipts(row)
        added = tuple(dict.fromkeys(receipt for receipt in incoming if receipt not in previous))
        if added and not row.transport_inflight:
            raise CompanionConflict("New receipts require a pending transport boundary")
        row.confirmed_receipts_json = dumps(list(previous + added))
        if added:
            row.transport_inflight = False
        member = await db.get(CompanionMember, (claim.scope_id, claim.persona_key, claim.member_ref))
        if member is not None:
            member.last_contact_at = max(member.last_contact_at or current_time, row.transport_started_at)
        if row.status != ACTIVE:
            await _waiting_reply(db, row, current_time)


async def _waiting_reply(db: Any, row: CompanionContact, now: datetime) -> None:
    member = await db.get(CompanionMember, (row.scope_id, row.persona_key, row.member_ref))
    if member is None or member.version != row.member_version:
        return
    proposal = contact_proposal(row.proposal_json)
    evidence = set(proposal.evidence_turn_ids)
    attention = (
        await db.scalars(
            select(CompanionAttention).where(
                CompanionAttention.scope_id == row.scope_id,
                CompanionAttention.persona_key == row.persona_key,
                CompanionAttention.member_ref == row.member_ref,
            )
        )
    ).all()
    changed = False
    for item in attention:
        state = attention_state(item.state_json)
        if state.member_ref != item.member_ref or state.key != item.key:
            raise CompanionCorruptState("Persisted attention does not match its bound member")
        if (
            state.kind == "concern"
            and state.status == "open"
            and (state.key == proposal.key or bool(evidence.intersection(state.evidence_turn_ids)))
        ):
            value = state.model_dump(mode="json")
            value["status"] = "waiting_reply"
            item.state_json, item.updated_at = dumps(value), now
            item.version += 1
            changed = True
    if changed:
        mode = await db.get(CompanionMode, row.scope_id)
        if mode is not None and mode.persona_key == row.persona_key:
            if mode.review_token is not None:
                review = await db.get(CompanionReview, mode.review_token)
                if review is not None and review.status == ACTIVE:
                    review.status, review.reason, review.finished_at = "cancelled", "confirmed_contact_feedback", now
                mode.review_token = mode.review_deadline = None
            mode.context_revision += 1
            mode.updated_at = now


async def finish_contact(claim: ContactClaim, *, status: str, now: datetime, reason: str = "") -> bool:
    if status not in FINAL_STATUSES:
        raise ValueError("Invalid contact outcome")
    bounded(reason, "reason", 1000, required=False)
    current_time = naive(now)
    async with transaction() as db:
        row = await db.get(CompanionContact, claim.attempt_ref)
        if not bound_contact(row, claim):
            return False
        confirmed = receipts(row)
        if status == "delivered" and not confirmed:
            raise CompanionConflict("Delivered contacts require actual confirmed message IDs")
        if row.transport_inflight:
            outcome = "unknown"
        elif confirmed:
            outcome = "delivered"
        elif row.transport_started_at is not None:
            outcome = "unknown"
        else:
            outcome = status
        if confirmed:
            await _waiting_reply(db, row, current_time)
        if row.status != ACTIVE:
            if row.status == "unknown" and outcome != "delivered":
                return row.status == outcome
            if row.status not in {"unknown", "cancelled"} or not confirmed:
                return row.status == outcome
        row.status, row.reason, row.finished_at = outcome, reason, current_time
        row.lease_deadline = current_time
        return True
