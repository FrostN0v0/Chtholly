"""Capture complete, attributable interaction evidence after turn finalization."""

from __future__ import annotations

import json
from datetime import datetime
from collections.abc import Mapping

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from entari_plugin_database import get_session

from .audit import utc_text, pending_payload
from ..models import AgentTurn, ChatScope, AgentEvent, ContextSession, RelationshipEvidence
from .identity import resolve_relationship_owner
from ..core.media import sanitize_assistant_history
from ..core.types import JSONType
from ..companion.evidence import member_input_text, member_input_events, eligible_member_inputs

_ELIGIBLE_OUTCOMES = frozenset({"completed", "silent", "declined"})


def _input_text(payload: Mapping[str, object]) -> str:
    content = member_input_text(payload)
    if content is None:
        raise ValueError("Completed interaction has no recorded attributable user input")
    return content


async def enqueue_relationship_evidence(
    *,
    turn_id: int,
    user_id: str,
    channel_id: str,
    persona_prompt: str,
    model: str,
    subject_user_id: str | None = None,
) -> bool:
    async with get_session() as db:
        row = (
            await db.execute(
                select(AgentTurn, ChatScope, ContextSession)
                .join(ContextSession, ContextSession.id == AgentTurn.session_id)
                .join(ChatScope, ChatScope.id == ContextSession.scope_id)
                .where(AgentTurn.id == turn_id)
            )
        ).one_or_none()
        if row is None:
            return False
        turn, scope, context_session = row
        if (
            (turn.trigger_kind, context_session.purpose) not in {("message", "chat"), ("autonomous", "companion")}
            or turn.status not in _ELIGIBLE_OUTCOMES
            or turn.finished_at is None
        ):
            return False
        owner = await resolve_relationship_owner(db, user_id, channel_id)
        subject = turn.user_id if subject_user_id is None else subject_user_id
        if not subject:
            return False
        original_owner = await resolve_relationship_owner(db, subject, channel_id)
        if original_owner != owner or scope.channel_id != channel_id:
            raise ValueError("Relationship evidence does not belong to the captured member and channel")
        evidence_key = (turn_id, subject)
        if await db.get(RelationshipEvidence, evidence_key) is not None:
            return False
        events = await member_input_events(db, turn_id)
        host_attributed = any(
            isinstance(payload := json.loads(event.payload_json), Mapping) and "host_author" in payload
            for event in events
            if event.event_type == "user_input"
        )
        if (
            not host_attributed
            and await db.scalar(
                select(RelationshipEvidence.turn_id).where(
                    RelationshipEvidence.turn_id == turn_id,
                    RelationshipEvidence.user_id == owner,
                )
            )
            is not None
        ):
            # The old single-owner schema may already have been canonicalized
            # before its immutable source key was introduced.
            return False
        user_inputs = [
            _input_text(payload)
            for payload, author in eligible_member_inputs(
                events, turn.user_id if turn.trigger_kind == "message" else ""
            )
            if author == subject
        ]
        if not user_inputs:
            if turn.trigger_kind == "message" and subject_user_id is None and not host_attributed:
                raise ValueError("Completed interaction is missing its user input event")
            return False
        decision_event = next((event for event in reversed(events) if event.event_type == "response_decision"), None)
        if decision_event is None:
            return False
        decision = json.loads(decision_event.payload_json)
        if not isinstance(decision, dict):
            raise ValueError("Recorded response decision is not an object")
        delivery = decision.get("actual_delivery")
        if not isinstance(delivery, dict):
            raise ValueError("Recorded response decision has no actual delivery evidence")
        confirmed = delivery.get("confirmed_deliveries")
        delivered_media = delivery.get("media_messages")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in (confirmed, delivered_media)
        ):
            raise ValueError("Recorded response decision has invalid delivery counts")
        expected = {"silent": {"silent"}, "declined": {"declined"}, "completed": {"automatic", "delivered"}}
        if decision.get("outcome") not in expected[turn.status]:
            raise ValueError("Response decision does not match the finalized outcome")
        if (turn.status == "silent") != (confirmed == 0):
            raise ValueError("Response decision does not match confirmed delivery")
        episode: dict[str, JSONType] = {
            "turn_id": turn_id,
            "subject_user_id": subject,
            "user": "\n\n".join(user_inputs),
            "assistant": sanitize_assistant_history(turn.final_text) if turn.final_text else "",
            "outcome": turn.status,
            "delivered_media": delivered_media,
            "received_at": utc_text(turn.created_at),
        }
        now = datetime.utcnow()
        sequence = (
            await db.scalar(select(func.max(AgentEvent.sequence)).where(AgentEvent.turn_id == turn_id)) or 0
        ) + 1
        event = AgentEvent(
            turn_id=turn_id,
            sequence=sequence,
            event_type="relationship_evaluation",
            role="assistant",
            payload_json=json.dumps(
                {
                    **pending_payload(turn_id=turn_id, model=model, queued_at=now),
                    "subject_user_id": subject,
                    "owner_user_id": owner,
                }
            ),
            status="pending",
            effect="none",
            model_visible=False,
            created_at=now,
        )
        db.add(event)
        try:
            await db.flush()
            db.add(
                RelationshipEvidence(
                    turn_id=turn_id,
                    subject_user_id=subject,
                    user_id=owner,
                    channel_id=channel_id,
                    persona_prompt=persona_prompt,
                    payload_json=json.dumps(episode, ensure_ascii=False, separators=(",", ":")),
                    event_id=event.id,
                    queued_at=now,
                )
            )
            await db.commit()
        except IntegrityError:
            await db.rollback()
            if await db.get(RelationshipEvidence, evidence_key) is not None:
                return False
            raise
        return True


async def enqueue_companion_relationship_evidence(*, turn_id: int, persona_prompt: str, model: str) -> int:
    """Queue directed or confirmed-engaged real members from one genuine turn."""
    async with get_session() as db:
        events = await member_input_events(db, turn_id)
        channel_id = await db.scalar(
            select(ChatScope.channel_id)
            .join(ContextSession, ContextSession.scope_id == ChatScope.id)
            .join(AgentTurn, AgentTurn.session_id == ContextSession.id)
            .where(AgentTurn.id == turn_id)
        )
    if channel_id is None:
        return 0
    members = {
        subject: channel_id for payload, subject in eligible_member_inputs(events, "") if "host_author" in payload
    }
    queued = 0
    for subject, channel_id in members.items():
        queued += await enqueue_relationship_evidence(
            turn_id=turn_id,
            user_id=subject,
            subject_user_id=subject,
            channel_id=channel_id,
            persona_prompt=persona_prompt,
            model=model,
        )
    return queued
