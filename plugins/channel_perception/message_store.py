"""Ambient-message write persistence for channel perception."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, select, update
from entari_plugin_database import get_session

from .config import ChannelPerceptionConfig
from .models import AmbientMessage, ChannelParticipant
from .schemas import MessageMutation, MessageObservation, ParticipantObservation
from .participant_store import upsert_participant

MAX_RETENTION_DAYS = 30
MAX_MESSAGES_PER_CHANNEL = 5_000
MAX_PARTICIPANT_RETENTION_DAYS = 365
MAX_PARTICIPANTS_PER_CHANNEL = 5_000


async def _record_message(observation: MessageObservation) -> None:
    if not observation.message_id:
        return
    participant = await upsert_participant(observation.participant)
    scope = observation.participant.scope
    async with get_session() as session:
        row = (
            await session.execute(
                select(AmbientMessage).where(
                    AmbientMessage.platform == scope.platform,
                    AmbientMessage.account_id == scope.account_id,
                    AmbientMessage.channel_id == scope.channel_id,
                    AmbientMessage.message_id == observation.message_id,
                )
            )
        ).scalar_one_or_none()
        values = {
            "guild_id": scope.guild_id,
            "person_id": participant.person_id,
            "participant_ref": participant.public_ref,
            "display_name": observation.display_name,
            "content": observation.message.content,
            "reply_to_message_id": observation.message.reply_to_message_id,
            "image_count": observation.message.image_count,
            "directed_to_bot": observation.directed_to_bot,
            "is_command": observation.is_command,
            "is_bot": observation.is_bot,
            "updated_at": observation.observed_at,
            "deleted_at": None,
        }
        if row is None:
            row = AmbientMessage(
                platform=scope.platform,
                account_id=scope.account_id,
                channel_id=scope.channel_id,
                message_id=observation.message_id,
                created_at=observation.observed_at,
                **values,
            )
            session.add(row)
        else:
            for key, value in values.items():
                setattr(row, key, value)
        await session.commit()


async def _mutate_message(mutation: MessageMutation) -> None:
    values: dict[str, object] = {"updated_at": mutation.observed_at}
    if mutation.kind == "message_delete":
        values.update(
            content="",
            reply_to_message_id="",
            image_count=0,
            deleted_at=mutation.observed_at,
        )
    elif mutation.message is not None:
        values.update(
            content=mutation.message.content,
            reply_to_message_id=mutation.message.reply_to_message_id,
            image_count=mutation.message.image_count,
            deleted_at=None,
        )
    async with get_session() as session:
        await session.execute(
            update(AmbientMessage)
            .where(
                AmbientMessage.platform == mutation.scope.platform,
                AmbientMessage.account_id == mutation.scope.account_id,
                AmbientMessage.channel_id == mutation.scope.channel_id,
                AmbientMessage.message_id == mutation.message_id,
            )
            .values(**values)
        )
        await session.commit()


async def prune_scope(scope, config: ChannelPerceptionConfig, now: datetime) -> None:
    retention_days = min(MAX_RETENTION_DAYS, max(1, int(config.retention_days)))
    retained_messages = min(MAX_MESSAGES_PER_CHANNEL, max(1, int(config.max_messages_per_channel)))
    participant_retention_days = max(
        retention_days,
        min(MAX_PARTICIPANT_RETENTION_DAYS, max(1, int(config.participant_retention_days))),
    )
    retained_participants = min(
        MAX_PARTICIPANTS_PER_CHANNEL,
        max(1, int(config.max_participants_per_channel)),
    )
    message_cutoff = now - timedelta(days=retention_days)
    participant_cutoff = now - timedelta(days=participant_retention_days)
    async with get_session() as session:
        message_scope = (
            AmbientMessage.platform == scope.platform,
            AmbientMessage.account_id == scope.account_id,
            AmbientMessage.channel_id == scope.channel_id,
        )
        await session.execute(delete(AmbientMessage).where(*message_scope, AmbientMessage.created_at < message_cutoff))
        stale_message_ids = (
            (
                await session.execute(
                    select(AmbientMessage.id)
                    .where(*message_scope)
                    .order_by(AmbientMessage.created_at.desc(), AmbientMessage.id.desc())
                    .offset(retained_messages)
                )
            )
            .scalars()
            .all()
        )
        if stale_message_ids:
            await session.execute(delete(AmbientMessage).where(AmbientMessage.id.in_(stale_message_ids)))

        participant_scope = (
            ChannelParticipant.platform == scope.platform,
            ChannelParticipant.account_id == scope.account_id,
            ChannelParticipant.channel_id == scope.channel_id,
        )
        await session.execute(
            delete(ChannelParticipant).where(
                *participant_scope,
                ChannelParticipant.last_seen_at < participant_cutoff,
            )
        )
        stale_participant_ids = (
            (
                await session.execute(
                    select(ChannelParticipant.id)
                    .where(*participant_scope)
                    .order_by(ChannelParticipant.last_seen_at.desc(), ChannelParticipant.id.desc())
                    .offset(retained_participants)
                )
            )
            .scalars()
            .all()
        )
        if stale_participant_ids:
            await session.execute(delete(ChannelParticipant).where(ChannelParticipant.id.in_(stale_participant_ids)))
        await session.commit()


async def store_observation(
    observation: MessageObservation | MessageMutation | ParticipantObservation,
    config: ChannelPerceptionConfig,
) -> None:
    if isinstance(observation, ParticipantObservation):
        await upsert_participant(observation)
        await prune_scope(observation.scope, config, observation.observed_at)
        return
    if isinstance(observation, MessageMutation):
        await _mutate_message(observation)
        await prune_scope(observation.scope, config, observation.observed_at)
        return
    await _record_message(observation)
    await prune_scope(observation.participant.scope, config, observation.observed_at)
