"""Move affect and pending evidence only for explicitly merged user identities."""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AgentEvent, UserAffect, RelationshipEvidence, RelationshipIdentity


async def resolve_relationship_owner(db: AsyncSession, user_id: str, channel_id: str) -> str:
    seen: set[str] = set()
    while user_id not in seen and len(seen) < 16:
        seen.add(user_id)
        mapping = await db.get(RelationshipIdentity, (user_id, channel_id))
        if mapping is None:
            return user_id
        user_id = mapping.target_user_id
    raise ValueError("Relationship identity mapping contains a cycle or excessive depth")


async def merge_relationship_identity(
    db: AsyncSession,
    channel_id: str,
    source_ids: set[str],
    target_id: str,
    *,
    relation_changed: bool,
) -> None:
    await db.execute(
        delete(RelationshipIdentity).where(
            RelationshipIdentity.source_user_id == target_id,
            RelationshipIdentity.channel_id == channel_id,
        )
    )
    await db.execute(
        update(RelationshipIdentity)
        .where(
            RelationshipIdentity.target_user_id.in_(source_ids),
            RelationshipIdentity.channel_id == channel_id,
        )
        .values(target_user_id=target_id, updated_at=datetime.utcnow())
    )
    for source_id in source_ids:
        mapping = await db.get(RelationshipIdentity, (source_id, channel_id))
        if mapping is None:
            db.add(RelationshipIdentity(source_user_id=source_id, channel_id=channel_id, target_user_id=target_id))
        else:
            mapping.target_user_id = target_id
            mapping.updated_at = datetime.utcnow()
    rows = list(
        (
            await db.execute(
                select(UserAffect).where(
                    UserAffect.channel_id == channel_id,
                    UserAffect.user_id.in_([target_id, *source_ids]),
                )
            )
        ).scalars()
    )
    has_evidence = await db.scalar(
        select(RelationshipEvidence.turn_id)
        .where(RelationshipEvidence.channel_id == channel_id, RelationshipEvidence.user_id.in_(source_ids))
        .limit(1)
    )
    if not relation_changed and not any(row.user_id in source_ids for row in rows) and has_evidence is None:
        return
    target = next((row for row in rows if row.user_id == target_id), None)
    if rows:
        latest = max(rows, key=lambda row: (row.updated_at, row.version))
        version = max(row.version for row in rows) + 1
        watermark = max(row.processed_turn_id for row in rows)
        if target is None:
            target = latest
            target.user_id = target_id
        elif latest is not target:
            target.description = latest.description
            target.emotions_json = latest.emotions_json
        target.version = version
        target.processed_turn_id = watermark
        target.updated_at = datetime.utcnow()
        stale_ids = [row.user_id for row in rows if row is not target]
        if stale_ids:
            await db.execute(
                delete(UserAffect).where(UserAffect.channel_id == channel_id, UserAffect.user_id.in_(stale_ids))
            )
    await db.execute(
        update(RelationshipEvidence)
        .where(RelationshipEvidence.channel_id == channel_id, RelationshipEvidence.user_id.in_(source_ids))
        .values(user_id=target_id)
    )
    running = list(
        (
            await db.execute(
                select(RelationshipEvidence).where(
                    RelationshipEvidence.channel_id == channel_id,
                    RelationshipEvidence.user_id == target_id,
                    RelationshipEvidence.status == "running",
                )
            )
        ).scalars()
    )
    for evidence in running:
        evidence.status = "pending"
        evidence.error = "Identity binding changed before evaluation commit"
        event = await db.get(AgentEvent, evidence.event_id)
        if event is not None:
            payload = json.loads(event.payload_json)
            payload.update(after=None, error=evidence.error, finished_at=None)
            event.payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            event.status = "pending"
            event.effect = "none"
