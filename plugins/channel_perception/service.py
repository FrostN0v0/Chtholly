"""Launart service for non-blocking channel perception."""

from __future__ import annotations

import asyncio
from datetime import datetime

from launart import Launart, Service
from arclet.entari import Session
from launart.status import Phase
from arclet.entari.logger import log

from .core import clean_text, collect_image_sources
from .config import ChannelPerceptionConfig
from .queries import (
    get_participant,
    find_participants,
    get_ambient_context,
    get_recent_messages,
    get_message_image_target,
)
from .schemas import (
    MessageView,
    FlushBarrier,
    MessageMutation,
    ParticipantView,
    PerceptionScope,
    MessageObservation,
    ParticipantSnapshot,
    ParticipantObservation,
)
from .message_store import store_observation
from .participant_store import upsert_participant, update_avatar_observation

_LOGGER = log.wrapper("[channel_perception]")
Observation = MessageObservation | MessageMutation | ParticipantObservation
MAX_QUEUE_SIZE = 10_000
MAX_PROTOCOL_MEMBER_SCAN = 5_000


def scope_from_session(session: Session) -> PerceptionScope:
    guild = getattr(session, "guild", None)
    return PerceptionScope(
        platform=session.account.platform,
        account_id=session.account.self_id,
        guild_id=clean_text(getattr(guild, "id", None)),
        channel_id=session.channel.id,
    )


def participant_from_session(session: Session, observed_at: datetime) -> ParticipantObservation:
    member = session.member
    user = session.user
    return ParticipantObservation(
        scope=scope_from_session(session),
        platform_user_id=clean_text(user.id),
        platform_nickname=clean_text(user.name),
        group_card=clean_text(member.nick if member else None),
        avatar_url=clean_text((member.avatar if member else None) or user.avatar),
        observed_at=observed_at,
    )


class ChannelPerceptionService(Service):
    id = "channel_perception.service"

    def __init__(self, config: ChannelPerceptionConfig) -> None:
        super().__init__()
        self.config = config
        self._queue: asyncio.Queue[Observation | FlushBarrier | None] = asyncio.Queue(
            maxsize=min(MAX_QUEUE_SIZE, max(1, int(config.queue_size)))
        )
        self._worker: asyncio.Task[None] | None = None
        self._dropped = 0

    @property
    def required(self) -> set[str]:
        return {"database/sqlalchemy"}

    @property
    def stages(self) -> set[Phase]:
        return {"preparing", "blocking", "cleanup"}

    def enqueue(self, observation: Observation) -> bool:
        try:
            self._queue.put_nowait(observation)
        except asyncio.QueueFull:
            self._dropped += 1
            if self._dropped == 1 or self._dropped % 100 == 0:
                _LOGGER.warning(f"observation queue full; dropped={self._dropped}")
            return False
        return True

    async def flush(self) -> None:
        worker = self._worker
        if worker is None or worker.done():
            return
        future = asyncio.get_running_loop().create_future()
        await self._queue.put(FlushBarrier(future))
        await asyncio.wait_for(future, timeout=10.0)

    async def _run_worker(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item is None:
                    return
                if isinstance(item, FlushBarrier):
                    if not item.future.done():
                        item.future.set_result(None)
                    continue
                try:
                    await store_observation(item, self.config)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    _LOGGER.warning(f"observation persistence failed: {type(exc).__name__}")
            finally:
                self._queue.task_done()

    async def resolve_current_participant(self, session: Session) -> ParticipantSnapshot:
        return await upsert_participant(participant_from_session(session, datetime.utcnow()))

    async def refresh_participant(self, session: Session, public_ref: str) -> ParticipantSnapshot | None:
        scope = scope_from_session(session)
        participant = await get_participant(scope, public_ref)
        if participant is None:
            return None
        try:
            member = await session.guild_member_get(participant.platform_user_id)
        except Exception:
            return participant
        user = member.user
        if user is None:
            return participant
        return await upsert_participant(
            ParticipantObservation(
                scope=scope,
                platform_user_id=clean_text(user.id),
                platform_nickname=clean_text(user.name),
                group_card=clean_text(member.nick),
                avatar_url=clean_text(member.avatar or user.avatar),
                observed_at=datetime.utcnow(),
            )
        )

    async def recent_messages(
        self,
        session: Session,
        *,
        limit: int,
        before_cursor: str = "",
        participant_ref: str = "",
    ) -> tuple[list[MessageView], str]:
        await self.flush()
        return await get_recent_messages(
            scope_from_session(session),
            limit=limit,
            before_cursor=before_cursor,
            participant_ref=participant_ref,
        )

    async def message_image_sources(self, session: Session, cursor: str) -> list[str]:
        await self.flush()
        target = await get_message_image_target(scope_from_session(session), cursor)
        if target is None:
            return []
        message_id, image_count = target
        if image_count == 0:
            return []
        message = await session.message_get(message_id)
        return collect_image_sources(message.message)[:image_count]

    async def ambient_context(
        self,
        session: Session,
        *,
        max_messages: int,
        max_chars: int,
        exclude_message_id: str = "",
    ) -> list[dict[str, object]]:
        await self.flush()
        return await get_ambient_context(
            scope_from_session(session),
            max_messages=max_messages,
            max_chars=max_chars,
            exclude_message_id=exclude_message_id,
        )

    async def _refresh_matching_participants(
        self,
        session: Session,
        query: str,
        *,
        limit: int,
    ) -> None:
        normalized_query = query.strip().casefold()
        if not normalized_query:
            return
        scope = scope_from_session(session)
        observed_at = datetime.utcnow()
        matched = 0
        scanned = 0
        match_limit = min(10, max(1, int(limit)))
        async for member in session.guild_member_list():
            scanned += 1
            if scanned > MAX_PROTOCOL_MEMBER_SCAN:
                break
            user = member.user
            if user is None:
                continue
            platform_user_id = clean_text(user.id)
            platform_nickname = clean_text(user.name)
            group_card = clean_text(member.nick)
            names = (platform_user_id, platform_nickname, group_card)
            if not platform_user_id or not any(normalized_query in name.casefold() for name in names if name):
                continue
            await upsert_participant(
                ParticipantObservation(
                    scope=scope,
                    platform_user_id=platform_user_id,
                    platform_nickname=platform_nickname,
                    group_card=group_card,
                    avatar_url=clean_text(member.avatar or user.avatar),
                    observed_at=observed_at,
                )
            )
            matched += 1
            if matched >= match_limit:
                break

    async def find_participants(
        self,
        session: Session,
        query: str,
        *,
        limit: int,
    ) -> list[ParticipantView]:
        await self.flush()
        bounded_limit = min(10, max(1, int(limit)))
        scope = scope_from_session(session)
        rows = await find_participants(scope, query, limit=bounded_limit)
        if not query.strip() or len(rows) >= bounded_limit:
            return rows
        try:
            await self._refresh_matching_participants(session, query, limit=bounded_limit)
        except Exception:
            fallback = await find_participants(scope, query, limit=bounded_limit)
            if fallback:
                return fallback
            raise
        return await find_participants(scope, query, limit=bounded_limit)

    async def update_avatar(
        self,
        session: Session,
        public_ref: str,
        *,
        expected_avatar_url: str,
        avatar_hash: str,
        avatar_description: str,
        observed_at: datetime,
    ) -> None:
        await update_avatar_observation(
            scope_from_session(session),
            public_ref,
            expected_avatar_url=expected_avatar_url,
            avatar_hash=avatar_hash,
            avatar_description=avatar_description,
            observed_at=observed_at,
        )

    async def launch(self, manager: Launart):
        async with self.stage("preparing"):
            self._worker = asyncio.create_task(self._run_worker(), name="channel-perception-writer")
        async with self.stage("blocking"):
            await manager.status.wait_for_sigexit()
        async with self.stage("cleanup"):
            try:
                await self.flush()
            except Exception as exc:
                _LOGGER.warning(f"observation flush failed during cleanup: {type(exc).__name__}")
            worker, self._worker = self._worker, None
            if worker is not None and not worker.done():
                await self._queue.put(None)
                try:
                    await asyncio.wait_for(worker, timeout=5.0)
                except TimeoutError:
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
