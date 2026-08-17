"""Typed Launart boundary for channel-perception capabilities."""

from __future__ import annotations

from typing import Protocol, cast
from datetime import datetime
from collections.abc import Callable

from launart import Launart
from arclet.entari import Session


class ParticipantSnapshotLike(Protocol):
    person_id: int
    previous_person_ids: tuple[int, ...]
    public_ref: str
    platform_user_id: str
    display_name: str
    avatar_url: str
    avatar_hash: str
    avatar_description: str


class ChannelPerceptionLike(Protocol):
    async def resolve_current_participant(self, session: Session) -> ParticipantSnapshotLike: ...

    async def refresh_participant(
        self,
        session: Session,
        public_ref: str,
    ) -> ParticipantSnapshotLike | None: ...

    async def recent_messages(
        self,
        session: Session,
        *,
        limit: int,
        before_cursor: str = "",
        participant_ref: str = "",
    ) -> tuple[list[dict[str, object]], str]: ...

    async def ambient_context(
        self,
        session: Session,
        *,
        max_messages: int,
        max_chars: int,
        exclude_message_id: str = "",
    ) -> list[dict[str, object]]: ...

    async def find_participants(
        self,
        session: Session,
        query: str,
        *,
        limit: int,
    ) -> list[dict[str, object]]: ...

    async def update_avatar(
        self,
        session: Session,
        public_ref: str,
        *,
        expected_avatar_url: str,
        avatar_hash: str,
        avatar_description: str,
        observed_at: datetime,
    ) -> None: ...


PerceptionProvider = Callable[[], ChannelPerceptionLike]


def get_channel_perception() -> ChannelPerceptionLike:
    """Resolve the registered service without importing another plugin's internals."""

    return cast(
        ChannelPerceptionLike,
        Launart.current().get_component("channel_perception.service"),
    )
