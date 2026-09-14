"""Atomic evaluation claims, versioned state commits and cancellation recovery."""

from __future__ import annotations

import json
from typing import cast
from secrets import token_hex
from datetime import datetime

from sqlalchemy import case, select, update
from entari_plugin_database import get_session

from utils.relationship_core.models import AXIS_KEYS
from utils.relationship_core.policy import serialize_emotions

from .audit import utc_text, evaluation_payload, update_evaluation_events
from .state import ensure_affect, snapshot_relationship
from .types import EvaluationBatch, RelationshipConflict
from ..models import UserAffect, UserRelation, RelationshipEvidence
from .identity import resolve_relationship_owner
from ..core.eval import EvalResult
from ..core.types import JSONType
from ..persona.memory_update import PreparedMemoryUpdates, apply_memory_updates

_MAX_BATCH_CHARS = 96_000


async def claim_evaluation(
    user_id: str,
    channel_id: str,
    *,
    batch_size: int,
    model: str,
) -> EvaluationBatch | None:
    async with get_session() as db:
        user_id = await resolve_relationship_owner(db, user_id, channel_id)
        affect = await ensure_affect(db, user_id, channel_id)
        busy = await db.scalar(
            select(RelationshipEvidence.turn_id)
            .where(
                RelationshipEvidence.user_id == user_id,
                RelationshipEvidence.channel_id == channel_id,
                RelationshipEvidence.status == "running",
            )
            .limit(1)
        )
        if busy is not None:
            await db.commit()
            return None
        candidates = list(
            (
                await db.execute(
                    select(RelationshipEvidence)
                    .where(
                        RelationshipEvidence.user_id == user_id,
                        RelationshipEvidence.channel_id == channel_id,
                        RelationshipEvidence.status.in_(("pending", "failed")),
                    )
                    .order_by(RelationshipEvidence.turn_id)
                    .limit(max(1, min(16, batch_size)))
                )
            ).scalars()
        )
        if not candidates:
            await db.commit()
            return None
        selected: list[RelationshipEvidence] = []
        characters = 0
        for evidence in candidates:
            if evidence.persona_prompt != candidates[0].persona_prompt:
                break
            size = len(evidence.payload_json)
            if selected and characters + size > _MAX_BATCH_CHARS:
                break
            selected.append(evidence)
            characters += size
        relation = await db.get(UserRelation, (user_id, channel_id))
        if relation is None:
            raise LookupError("Relationship evaluation owner no longer exists")
        now = datetime.utcnow()
        batch = EvaluationBatch(
            evaluation_ref=f"evaluation_{token_hex(12)}",
            user_id=user_id,
            channel_id=channel_id,
            persona_prompt=selected[0].persona_prompt,
            turn_ids=tuple(item.turn_id for item in selected),
            event_ids=tuple(item.event_id for item in selected),
            episodes=tuple(cast(dict[str, JSONType], json.loads(item.payload_json)) for item in selected),
            before=snapshot_relationship(relation, affect, now=now),
            version=affect.version,
            queued_at=utc_text(selected[0].queued_at),
            started_at=now,
            model=model,
        )
        for evidence in selected:
            evidence.status = "running"
            evidence.evaluation_ref = batch.evaluation_ref
            evidence.claimed_at = now
            evidence.finished_at = None
            evidence.attempts += 1
            evidence.error = ""
        await update_evaluation_events(db, batch.event_ids, status="running", payload=evaluation_payload(batch))
        await db.commit()
        return batch


async def commit_evaluation(
    batch: EvaluationBatch,
    result: EvalResult,
    *,
    memory: PreparedMemoryUpdates | None = None,
) -> dict[str, JSONType]:
    if tuple(sorted(result.processed_turn_ids)) != tuple(sorted(batch.turn_ids)):
        raise ValueError("Relationship evaluation did not account for its complete evidence batch")
    now = datetime.utcnow()
    if memory is None and (result.profile_patches or result.memory_items):
        raise ValueError("Memory changes must be prepared before consuming relationship evidence")
    if memory is not None and (memory.user_id, memory.channel_id) != (batch.user_id, batch.channel_id):
        raise RelationshipConflict("Prepared memory belongs to a different evaluation owner")
    async with get_session() as db:
        affect = (
            await db.execute(
                update(UserAffect)
                .where(
                    UserAffect.user_id == batch.user_id,
                    UserAffect.channel_id == batch.channel_id,
                    UserAffect.version == batch.version,
                )
                .values(
                    version=UserAffect.version + 1,
                    description=result.relationship_description,
                    emotions_json=json.dumps(serialize_emotions(result.emotions), ensure_ascii=False),
                    processed_turn_id=case(
                        (UserAffect.processed_turn_id < max(batch.turn_ids), max(batch.turn_ids)),
                        else_=UserAffect.processed_turn_id,
                    ),
                    updated_at=now,
                )
                .returning(UserAffect)
            )
        ).scalar_one_or_none()
        if affect is None:
            raise RelationshipConflict("Relationship state changed while its evaluation was running")
        values: dict[str, object] = {"impression": result.impression, "last_interaction": now}
        for name in AXIS_KEYS:
            column = getattr(UserRelation, name)
            shifted = column + result.deltas[name]
            values[name] = case((shifted < 0.0, 0.0), (shifted > 100.0, 100.0), else_=shifted)
        relation = (
            await db.execute(
                update(UserRelation)
                .where(UserRelation.user_id == batch.user_id, UserRelation.channel_id == batch.channel_id)
                .values(**values)
                .returning(UserRelation)
            )
        ).scalar_one_or_none()
        if relation is None:
            raise RelationshipConflict("Relationship owner changed while its evaluation was running")
        if memory is not None:
            await apply_memory_updates(db, memory)
        settled = await db.execute(
            update(RelationshipEvidence)
            .where(
                RelationshipEvidence.turn_id.in_(batch.turn_ids),
                RelationshipEvidence.evaluation_ref == batch.evaluation_ref,
                RelationshipEvidence.status == "running",
            )
            .values(status="processed", finished_at=now, error="")
        )
        if getattr(settled, "rowcount", 0) != len(batch.turn_ids):
            raise RelationshipConflict("Relationship evidence claim was superseded")
        after = snapshot_relationship(relation, affect, now=now)
        await update_evaluation_events(
            db,
            batch.event_ids,
            status="succeeded",
            payload=evaluation_payload(batch, after=after, finished_at=now),
            duration_ms=int((now - batch.started_at).total_seconds() * 1000),
        )
        await db.commit()
        return after


async def release_evaluation(batch: EvaluationBatch, error: str, *, cancelled: bool = False) -> None:
    now = datetime.utcnow()
    async with get_session() as db:
        rows = (
            (
                await db.execute(
                    update(RelationshipEvidence)
                    .where(
                        RelationshipEvidence.turn_id.in_(batch.turn_ids),
                        RelationshipEvidence.evaluation_ref == batch.evaluation_ref,
                        RelationshipEvidence.status == "running",
                    )
                    .values(status="pending" if cancelled else "failed", finished_at=now, error=error[:400])
                    .returning(RelationshipEvidence.event_id)
                )
            )
            .scalars()
            .all()
        )
        if rows:
            await update_evaluation_events(
                db,
                rows,
                status="pending" if cancelled else "failed",
                payload=evaluation_payload(batch, error=error[:400], finished_at=now),
                duration_ms=int((now - batch.started_at).total_seconds() * 1000),
            )
        await db.commit()
