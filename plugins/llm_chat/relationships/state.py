"""Consistent relationship snapshots without emotion-to-response policy."""

from __future__ import annotations

import json
from typing import cast
from datetime import datetime, timezone
from dataclasses import replace

from sqlalchemy import exists, insert, select, literal
from entari_plugin_database import get_session
from sqlalchemy.ext.asyncio import AsyncSession

from utils.relationship_core.models import AXIS_KEYS
from utils.relationship_core.policy import read_emotions, decay_emotions, serialize_emotions

from .audit import utc_text
from ..models import UserAffect, UserRelation
from ..core.types import JSONType
from ..persona.store import get_relation


def snapshot_relationship(
    relation: UserRelation,
    affect: UserAffect | None,
    *,
    now: datetime | None = None,
) -> dict[str, JSONType]:
    moment = now or datetime.utcnow()
    timestamp = moment.replace(tzinfo=timezone.utc).timestamp()
    stored = read_emotions(json.loads(affect.emotions_json)) if affect is not None else ()
    # Rebase effective read values so downstream consumers never decay them twice.
    emotions = tuple(replace(item, updated_at=timestamp) for item in decay_emotions(stored, now=timestamp))
    return {
        "version": affect.version if affect is not None else 0,
        "axes": {name: round(float(getattr(relation, name)), 3) for name in AXIS_KEYS},
        "description": affect.description if affect is not None else "",
        "impression": relation.impression,
        "emotions": cast(JSONType, serialize_emotions(emotions)),
        "updated_at": utc_text(affect.updated_at) if affect is not None else None,
        "observed_at": utc_text(moment),
        "processed_turn_id": affect.processed_turn_id if affect is not None else 0,
    }


async def ensure_affect(db: AsyncSession, user_id: str, channel_id: str) -> UserAffect:
    values = {
        "user_id": user_id,
        "channel_id": channel_id,
        "version": 0,
        "description": "",
        "emotions_json": "[]",
        "processed_turn_id": 0,
        "updated_at": datetime.utcnow(),
    }
    present = exists().where(UserAffect.user_id == user_id, UserAffect.channel_id == channel_id)
    await db.execute(
        insert(UserAffect).from_select(
            list(values),
            select(*(literal(value).label(name) for name, value in values.items())).where(~present),
        )
    )
    result = await db.get(UserAffect, (user_id, channel_id))
    if result is None:
        raise LookupError("Relationship affect state could not be created")
    return result


async def load_relationship_snapshot(user_id: str, channel_id: str) -> tuple[UserRelation, dict[str, JSONType]]:
    await get_relation(user_id, channel_id)
    async with get_session() as db:
        row = (
            await db.execute(
                select(UserRelation, UserAffect)
                .outerjoin(
                    UserAffect,
                    (UserAffect.user_id == UserRelation.user_id) & (UserAffect.channel_id == UserRelation.channel_id),
                )
                .where(UserRelation.user_id == user_id, UserRelation.channel_id == channel_id)
            )
        ).one()
        relation, affect = row
        return relation, snapshot_relationship(relation, affect)
