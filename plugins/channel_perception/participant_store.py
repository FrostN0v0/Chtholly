"""Participant identity persistence for channel perception."""

from __future__ import annotations

import json
import asyncio
import secrets
from datetime import datetime

from sqlalchemy import select, update
from satori.model import User
from entari_plugin_user import get_user  # entari: plugin
from entari_plugin_database import get_session

from .core import display_name
from .models import ChannelParticipant
from .schemas import PerceptionScope, ParticipantSnapshot, ParticipantObservation

_UPSERT_LOCK: asyncio.Lock | None = None


def _get_upsert_lock() -> asyncio.Lock:
    global _UPSERT_LOCK
    if _UPSERT_LOCK is None:
        _UPSERT_LOCK = asyncio.Lock()
    return _UPSERT_LOCK


def _load_identity_history(value: str) -> tuple[list[int], list[str]]:
    try:
        raw = json.loads(value)
    except ValueError:
        return [], []
    if isinstance(raw, list):
        person_ids = [item for item in raw if isinstance(item, int) and item > 0]
        return person_ids, []
    if not isinstance(raw, dict):
        return [], []
    raw_person_ids = raw.get("person_ids", [])
    raw_names = raw.get("names", [])
    if not isinstance(raw_person_ids, list):
        raw_person_ids = []
    if not isinstance(raw_names, list):
        raw_names = []
    person_ids = [item for item in raw_person_ids if isinstance(item, int) and item > 0]
    names = [item.strip() for item in raw_names if isinstance(item, str) and item.strip()]
    return list(dict.fromkeys(person_ids)), list(dict.fromkeys(names))


def _dump_identity_history(person_ids: list[int], names: list[str]) -> str:
    return json.dumps(
        {
            "person_ids": list(dict.fromkeys(person_ids))[-5:],
            "names": list(dict.fromkeys(names))[-10:],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def participant_snapshot(row: ChannelParticipant) -> ParticipantSnapshot:
    previous_person_ids, previous_names = _load_identity_history(row.identity_history_json)
    return ParticipantSnapshot(
        person_id=row.person_id,
        previous_person_ids=tuple(previous_person_ids),
        previous_names=tuple(previous_names),
        public_ref=row.public_ref,
        platform_user_id=row.platform_user_id,
        platform_nickname=row.platform_nickname,
        group_card=row.group_card,
        display_name=display_name(row.group_card, row.platform_nickname, "member"),
        avatar_url=row.avatar_url,
        avatar_hash=row.avatar_hash,
        avatar_description=row.avatar_description,
        avatar_observed_at=row.avatar_observed_at,
        last_seen_at=row.last_seen_at,
    )


async def _new_public_ref(session) -> str:
    for _ in range(8):
        value = f"participant_{secrets.token_hex(5)}"
        if await session.scalar(select(ChannelParticipant.id).where(ChannelParticipant.public_ref == value)) is None:
            return value
    raise RuntimeError("Unable to allocate participant reference")


async def upsert_participant(observation: ParticipantObservation) -> ParticipantSnapshot:
    platform_user = User(
        id=observation.platform_user_id,
        name=observation.platform_nickname or None,
        avatar=observation.avatar_url or None,
    )
    unified_user = await get_user(observation.scope.platform, platform_user)
    async with _get_upsert_lock():
        async with get_session() as session:
            row = (
                await session.execute(
                    select(ChannelParticipant).where(
                        ChannelParticipant.platform == observation.scope.platform,
                        ChannelParticipant.account_id == observation.scope.account_id,
                        ChannelParticipant.channel_id == observation.scope.channel_id,
                        ChannelParticipant.platform_user_id == observation.platform_user_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                row = ChannelParticipant(
                    platform=observation.scope.platform,
                    account_id=observation.scope.account_id,
                    guild_id=observation.scope.guild_id,
                    channel_id=observation.scope.channel_id,
                    platform_user_id=observation.platform_user_id,
                    person_id=unified_user.id,
                    public_ref=await _new_public_ref(session),
                    platform_nickname=observation.platform_nickname,
                    group_card=observation.group_card,
                    avatar_url=observation.avatar_url,
                    first_seen_at=observation.observed_at,
                    last_seen_at=observation.observed_at,
                    observed_at=observation.observed_at,
                )
                session.add(row)
            else:
                previous_person_ids, previous_names = _load_identity_history(row.identity_history_json)
                if row.person_id != unified_user.id:
                    if row.person_id not in previous_person_ids:
                        previous_person_ids.append(row.person_id)
                    row.person_id = unified_user.id
                current_names = {observation.group_card, observation.platform_nickname, ""}
                for previous_name in (row.group_card, row.platform_nickname):
                    if previous_name and previous_name not in current_names and previous_name not in previous_names:
                        previous_names.append(previous_name)
                row.identity_history_json = _dump_identity_history(previous_person_ids, previous_names)
                row.guild_id = observation.scope.guild_id
                row.platform_nickname = observation.platform_nickname
                row.group_card = observation.group_card
                row.avatar_url = observation.avatar_url
                row.last_seen_at = max(row.last_seen_at, observation.observed_at)
                row.observed_at = observation.observed_at
            await session.commit()
            return participant_snapshot(row)


async def find_participant_by_platform_user(
    scope: PerceptionScope,
    platform_user_id: str,
) -> ParticipantSnapshot | None:
    async with get_session() as session:
        row = (
            await session.execute(
                select(ChannelParticipant).where(
                    ChannelParticipant.platform == scope.platform,
                    ChannelParticipant.account_id == scope.account_id,
                    ChannelParticipant.channel_id == scope.channel_id,
                    ChannelParticipant.platform_user_id == platform_user_id,
                )
            )
        ).scalar_one_or_none()
        return participant_snapshot(row) if row is not None else None


async def update_avatar_observation(
    scope: PerceptionScope,
    public_ref: str,
    *,
    expected_avatar_url: str,
    avatar_hash: str,
    avatar_description: str,
    observed_at: datetime,
) -> None:
    async with get_session() as session:
        await session.execute(
            update(ChannelParticipant)
            .where(
                ChannelParticipant.platform == scope.platform,
                ChannelParticipant.account_id == scope.account_id,
                ChannelParticipant.channel_id == scope.channel_id,
                ChannelParticipant.public_ref == public_ref,
                ChannelParticipant.avatar_url == expected_avatar_url,
            )
            .values(
                avatar_hash=avatar_hash,
                avatar_description=avatar_description,
                avatar_observed_at=observed_at,
            )
        )
        await session.commit()
