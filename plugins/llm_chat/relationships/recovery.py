"""Recover the narrow crash window between turn finalization and queue insertion."""

from __future__ import annotations

import json

from sqlalchemy import or_, and_, exists, select
from entari_plugin_database import get_session

from ..models import AgentTurn, ChatScope, AgentEvent, ContextSession, MigrationState
from .evidence import enqueue_relationship_evidence, enqueue_companion_relationship_evidence


async def recover_unqueued_evidence(model: str) -> int:
    async with get_session() as db:
        marker = await db.get(MigrationState, "relationship_affect_v1")
        if marker is None:
            raise RuntimeError("Relationship recovery requires an established migration boundary")
        boundary = json.loads(marker.details_json).get("cutover_turn_id")
        if isinstance(boundary, bool) or not isinstance(boundary, int):
            raise ValueError("Relationship migration boundary is invalid")
    recovered = 0
    after_turn = boundary
    while True:
        async with get_session() as db:
            decision_recorded = exists().where(
                AgentEvent.turn_id == AgentTurn.id,
                AgentEvent.event_type == "response_decision",
            )
            rows = (
                await db.execute(
                    select(AgentTurn.id, AgentTurn.user_id, ChatScope.channel_id)
                    .join(ContextSession, ContextSession.id == AgentTurn.session_id)
                    .join(ChatScope, ChatScope.id == ContextSession.scope_id)
                    .where(
                        AgentTurn.id > after_turn,
                        or_(
                            and_(AgentTurn.trigger_kind == "message", ContextSession.purpose == "chat"),
                            and_(AgentTurn.trigger_kind == "autonomous", ContextSession.purpose == "companion"),
                        ),
                        AgentTurn.status.in_(("completed", "silent", "declined")),
                        AgentTurn.finished_at.is_not(None),
                        decision_recorded,
                    )
                    .order_by(AgentTurn.id)
                    .limit(128)
                )
            ).all()
        if not rows:
            return recovered
        for turn_id, user_id, channel_id in rows:
            after_turn = turn_id
            async with get_session() as db:
                inputs = (
                    (
                        await db.execute(
                            select(AgentEvent.payload_json).where(
                                AgentEvent.turn_id == turn_id,
                                AgentEvent.event_type == "user_input",
                                AgentEvent.model_visible.is_(True),
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                if not inputs:
                    continue
                companion_inputs = any(
                    isinstance(json.loads(raw), dict) and "host_author" in json.loads(raw) for raw in inputs
                )
                raw = await db.scalar(
                    select(AgentEvent.payload_json)
                    .where(AgentEvent.turn_id == turn_id, AgentEvent.event_type == "context_snapshot")
                    .order_by(AgentEvent.sequence)
                    .limit(1)
                )
            if raw is None:
                raise ValueError("Unqueued relationship evidence lacks its captured persona")
            payload = json.loads(raw)
            persona = payload.get("persona") if isinstance(payload, dict) else None
            prompt = persona.get("prompt") if isinstance(persona, dict) else None
            if not isinstance(prompt, str):
                raise ValueError("Unqueued relationship evidence has no usable captured persona")
            if companion_inputs:
                recovered += await enqueue_companion_relationship_evidence(
                    turn_id=turn_id,
                    persona_prompt=prompt,
                    model=model,
                )
            else:
                recovered += await enqueue_relationship_evidence(
                    turn_id=turn_id,
                    user_id=user_id,
                    channel_id=channel_id,
                    persona_prompt=prompt,
                    model=model,
                )
