"""Capture complete, attributable interaction evidence after turn finalization."""

from __future__ import annotations

import json
from typing import cast
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

_ELIGIBLE_OUTCOMES = frozenset({"completed", "silent", "declined"})


def _input_text(payload: Mapping[str, JSONType]) -> str:
    content = payload.get("content")
    if not isinstance(content, str):
        raise ValueError("Completed interaction has no recorded attributable user input")
    return content


async def enqueue_relationship_evidence(
    *,
    turn_id: int,
    user_id: str,
    channel_id: str,
    persona_prompt: str,
    model: str,
) -> bool:
    async with get_session() as db:
        if await db.get(RelationshipEvidence, turn_id) is not None:
            return False
        row = (
            await db.execute(
                select(AgentTurn, ChatScope)
                .join(ContextSession, ContextSession.id == AgentTurn.session_id)
                .join(ChatScope, ChatScope.id == ContextSession.scope_id)
                .where(AgentTurn.id == turn_id)
            )
        ).one_or_none()
        if row is None:
            return False
        turn, scope = row
        if turn.status not in _ELIGIBLE_OUTCOMES or turn.finished_at is None:
            return False
        owner = await resolve_relationship_owner(db, user_id, channel_id)
        original_owner = await resolve_relationship_owner(db, turn.user_id, channel_id)
        if original_owner != owner or scope.channel_id != channel_id:
            raise ValueError("Relationship evidence does not belong to the captured member and channel")
        events = list(
            (
                await db.execute(
                    select(AgentEvent)
                    .where(
                        AgentEvent.turn_id == turn_id,
                        AgentEvent.event_type.in_(("user_input", "response_decision")),
                    )
                    .order_by(AgentEvent.sequence)
                )
            ).scalars()
        )
        user_event = next((event for event in events if event.event_type == "user_input"), None)
        if user_event is None:
            raise ValueError("Completed interaction is missing its user input event")
        user_payload = json.loads(user_event.payload_json)
        if not isinstance(user_payload, dict):
            raise ValueError("Recorded user input is not an object")
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
            "user": _input_text(cast(Mapping[str, JSONType], user_payload)),
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
            payload_json=json.dumps(pending_payload(turn_id=turn_id, model=model, queued_at=now)),
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
            if await db.get(RelationshipEvidence, turn_id) is not None:
                return False
            raise
        return True
