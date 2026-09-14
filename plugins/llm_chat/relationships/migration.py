"""One-time relationship schema cutover and interrupted-job recovery."""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import func, text, select
from entari_plugin_database import get_session

from ..models import AgentTurn, AgentEvent, MigrationState, RelationshipEvidence

_MIGRATION_KEY = "relationship_affect_v1"


async def initialize_relationship_store() -> None:
    async with get_session() as db:
        marker = await db.get(MigrationState, _MIGRATION_KEY)
        if marker is None:
            dialect = db.get_bind().dialect.name
            if dialect == "sqlite":
                columns = {row[1] for row in (await db.execute(text("PRAGMA table_info(chat_user_relations)"))).all()}
            else:
                columns = set(
                    (
                        await db.execute(
                            text(
                                "SELECT column_name FROM information_schema.columns "
                                "WHERE table_name = 'chat_user_relations'"
                            )
                        )
                    ).scalars()
                )
            if "eval_counter" in columns:
                await db.execute(text("ALTER TABLE chat_user_relations DROP COLUMN eval_counter"))
            last_turn = await db.scalar(select(func.max(AgentTurn.id))) or 0
            db.add(
                MigrationState(
                    key=_MIGRATION_KEY,
                    status="completed",
                    details_json=json.dumps({"cutover_turn_id": last_turn, "historical_evidence_replayed": False}),
                    updated_at=datetime.utcnow(),
                )
            )
        interrupted = list(
            (await db.execute(select(RelationshipEvidence).where(RelationshipEvidence.status == "running"))).scalars()
        )
        for evidence in interrupted:
            evidence.status = "pending"
            evidence.error = "Evaluation interrupted before state commit"
            event = await db.get(AgentEvent, evidence.event_id)
            if event is not None:
                payload = json.loads(event.payload_json)
                payload.update(after=None, error=evidence.error, finished_at=None)
                event.payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                event.status = "pending"
                event.effect = "none"
        await db.commit()


async def pending_relationship_owners() -> list[tuple[str, str]]:
    async with get_session() as db:
        rows = (
            await db.execute(
                select(RelationshipEvidence.user_id, RelationshipEvidence.channel_id)
                .where(RelationshipEvidence.status.in_(("pending", "failed")))
                .distinct()
            )
        ).all()
        return [(user_id, channel_id) for user_id, channel_id in rows]
