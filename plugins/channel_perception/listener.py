"""Entari event listeners feeding channel perception."""

from __future__ import annotations

from typing import Literal
from datetime import datetime, timezone

from arclet.entari import At, ChannelType, plugin
from arclet.entari.config import EntariConfig
from arclet.entari.event.base import (
    MessageCreatedEvent,
    MessageDeletedEvent,
    MessageUpdatedEvent,
    GuildMemberUpdatedEvent,
)
from arclet.entari.event.send import SendResponse

from .core import clean_text, display_name, normalize_message, is_prefixed_command
from .schemas import MessageMutation, PerceptionScope, MessageObservation, ParticipantObservation
from .service import ChannelPerceptionService


def _timestamp(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _is_public_channel(event) -> bool:
    channel = getattr(event, "channel", None)
    return channel is not None and channel.type is not ChannelType.DIRECT


def _scope(event) -> PerceptionScope:
    return PerceptionScope(
        platform=event.account.platform,
        account_id=event.account.self_id,
        guild_id=clean_text(event.guild.id if event.guild else None),
        channel_id=event.channel.id,
    )


def _participant(event, observed_at: datetime) -> ParticipantObservation:
    return ParticipantObservation(
        scope=_scope(event),
        platform_user_id=clean_text(event.user.id),
        platform_nickname=clean_text(event.user.name),
        group_card=clean_text(event.member.nick if event.member else None),
        avatar_url=clean_text((event.member.avatar if event.member else None) or event.user.avatar),
        observed_at=observed_at,
    )


def _directed_to_bot(event, is_notice_me: bool, is_reply_me: bool) -> bool:
    if is_notice_me or is_reply_me:
        return True
    return any(element.id == event.account.self_id for element in event.content.select(At) if element.id)


def _message_observation(
    event: MessageCreatedEvent | MessageUpdatedEvent,
    service: ChannelPerceptionService,
    kind: Literal["message", "message_update"],
    is_notice_me: bool,
    is_reply_me: bool,
) -> MessageObservation:
    observed_at = _timestamp(event.timestamp)
    normalized = normalize_message(event.content, max_chars=service.config.max_content_chars)
    basic = EntariConfig.instance.basic
    return MessageObservation(
        kind=kind,
        participant=_participant(event, observed_at),
        message_id=event.message.id,
        message=normalized,
        display_name=display_name(
            clean_text(event.member.nick if event.member else None),
            clean_text(event.user.name),
            "member",
        ),
        directed_to_bot=_directed_to_bot(event, is_notice_me, is_reply_me),
        is_command=is_prefixed_command(normalized.content, basic.prefix, basic.nickname),
        is_bot=event.user.id == event.account.self_id,
        observed_at=observed_at,
    )


@plugin.listen(MessageCreatedEvent, priority=100)
async def observe_message_created(
    event: MessageCreatedEvent,
    service: ChannelPerceptionService,
    is_notice_me: bool = False,
    is_reply_me: bool = False,
) -> None:
    if not _is_public_channel(event):
        return
    service.enqueue(_message_observation(event, service, "message", is_notice_me, is_reply_me))


@plugin.listen(MessageUpdatedEvent, priority=100)
async def observe_message_updated(
    event: MessageUpdatedEvent,
    service: ChannelPerceptionService,
    is_notice_me: bool = False,
    is_reply_me: bool = False,
) -> None:
    if not _is_public_channel(event):
        return
    service.enqueue(_message_observation(event, service, "message_update", is_notice_me, is_reply_me))


@plugin.listen(MessageDeletedEvent, priority=100)
async def observe_message_deleted(event: MessageDeletedEvent, service: ChannelPerceptionService) -> None:
    if not _is_public_channel(event):
        return
    service.enqueue(
        MessageMutation(
            kind="message_delete",
            scope=_scope(event),
            message_id=event.message.id,
            message=None,
            observed_at=_timestamp(event.timestamp),
        )
    )


@plugin.listen(GuildMemberUpdatedEvent, priority=100)
async def observe_member_updated(event: GuildMemberUpdatedEvent, service: ChannelPerceptionService) -> None:
    if not _is_public_channel(event):
        return
    service.enqueue(_participant(event, _timestamp(event.timestamp)))


@plugin.listen(SendResponse, priority=100)
async def observe_message_sent(event: SendResponse, service: ChannelPerceptionService) -> None:
    if not event.result or event.session is None:
        return
    source_event = event.session.event
    source_channel = getattr(source_event, "channel", None)
    if source_channel is None or source_channel.type is ChannelType.DIRECT:
        return
    observed_at = datetime.utcnow()
    scope = PerceptionScope(
        platform=event.account.platform,
        account_id=event.account.self_id,
        guild_id=clean_text(source_event.guild.id if source_event.guild else None),
        channel_id=source_channel.id,
    )
    bot = event.account.self_info.user
    participant = ParticipantObservation(
        scope=scope,
        platform_user_id=clean_text(bot.id),
        platform_nickname=clean_text(bot.name),
        group_card="",
        avatar_url=clean_text(bot.avatar),
        observed_at=observed_at,
    )
    normalized = normalize_message(event.message, max_chars=service.config.max_content_chars)
    for result in event.result:
        service.enqueue(
            MessageObservation(
                kind="send",
                participant=participant,
                message_id=result.id,
                message=normalized,
                display_name="bot",
                directed_to_bot=False,
                is_command=False,
                is_bot=True,
                observed_at=observed_at,
            )
        )
