"""Runtime data contracts for channel perception."""

from __future__ import annotations

from typing import Literal, TypedDict
from asyncio import Future
from datetime import datetime
from dataclasses import dataclass

from .core import NormalizedMessage

ObservationKind = Literal["message", "message_update", "send"]


@dataclass(frozen=True, slots=True)
class PerceptionScope:
    platform: str
    account_id: str
    guild_id: str
    channel_id: str


@dataclass(frozen=True, slots=True)
class ParticipantObservation:
    scope: PerceptionScope
    platform_user_id: str
    platform_nickname: str
    group_card: str
    avatar_url: str
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class MessageObservation:
    kind: ObservationKind
    participant: ParticipantObservation
    message_id: str
    message: NormalizedMessage
    display_name: str
    directed_to_bot: bool
    is_command: bool
    is_bot: bool
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class MessageMutation:
    kind: Literal["message_update", "message_delete"]
    scope: PerceptionScope
    message_id: str
    message: NormalizedMessage | None
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class ParticipantSnapshot:
    person_id: int
    previous_person_ids: tuple[int, ...]
    previous_names: tuple[str, ...]
    public_ref: str
    platform_user_id: str
    platform_nickname: str
    group_card: str
    display_name: str
    avatar_url: str
    avatar_hash: str
    avatar_description: str
    avatar_observed_at: datetime | None
    last_seen_at: datetime


@dataclass(frozen=True, slots=True)
class FlushBarrier:
    future: Future[None]


class ParticipantView(TypedDict):
    participant_ref: str
    display_name: str
    platform_nickname: str
    group_card: str
    last_seen_at: str
    avatar_available: bool


class MessageView(TypedDict):
    cursor: str
    participant_ref: str
    display_name: str
    content: str
    image_count: int
    created_at: str
    minutes_ago: int
    directed_to_bot: bool
    is_bot: bool
    reply_to_cursor: str
