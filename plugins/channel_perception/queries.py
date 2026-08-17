"""Read-side bounded views for channel perception."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select
from entari_plugin_database import get_session

from .models import AmbientMessage, ChannelParticipant
from .schemas import MessageView, ParticipantView, PerceptionScope, ParticipantSnapshot
from .participant_store import participant_snapshot


def _scope_filters(model, scope: PerceptionScope):
    return (
        model.platform == scope.platform,
        model.account_id == scope.account_id,
        model.channel_id == scope.channel_id,
    )


def _utc_iso(value: datetime) -> str:
    current = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return current.isoformat(timespec="seconds").replace("+00:00", "Z")


def _minutes_ago(value: datetime, now: datetime) -> int:
    current = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    reference = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
    return max(0, int((reference - current).total_seconds() // 60))


def _participant_view(row: ChannelParticipant) -> ParticipantView:
    snapshot = participant_snapshot(row)
    return {
        "participant_ref": snapshot.public_ref,
        "display_name": snapshot.display_name,
        "platform_nickname": snapshot.platform_nickname,
        "group_card": snapshot.group_card,
        "last_seen_at": _utc_iso(snapshot.last_seen_at),
        "avatar_available": bool(snapshot.avatar_url),
    }


async def get_participant(scope: PerceptionScope, public_ref: str) -> ParticipantSnapshot | None:
    async with get_session() as session:
        row = (
            await session.execute(
                select(ChannelParticipant).where(
                    *_scope_filters(ChannelParticipant, scope),
                    ChannelParticipant.public_ref == public_ref,
                )
            )
        ).scalar_one_or_none()
        return participant_snapshot(row) if row is not None else None


async def find_participants(
    scope: PerceptionScope,
    query: str,
    *,
    limit: int,
) -> list[ParticipantView]:
    normalized = query.strip().casefold()
    bounded_limit = min(10, max(1, int(limit)))
    async with get_session() as session:
        rows = list(
            (
                await session.execute(
                    select(ChannelParticipant)
                    .where(*_scope_filters(ChannelParticipant, scope))
                    .order_by(ChannelParticipant.last_seen_at.desc(), ChannelParticipant.id.desc())
                    .limit(200)
                )
            )
            .scalars()
            .all()
        )
    if normalized:
        matched: list[ChannelParticipant] = []
        for row in rows:
            snapshot = participant_snapshot(row)
            names = (
                snapshot.public_ref,
                snapshot.platform_nickname,
                snapshot.group_card,
                *snapshot.previous_names,
            )
            if any(normalized in value.casefold() for value in names if value):
                matched.append(row)
                if len(matched) >= bounded_limit:
                    break
        rows = matched
    else:
        rows = rows[:bounded_limit]
    return [_participant_view(row) for row in rows]


async def get_recent_messages(
    scope: PerceptionScope,
    *,
    limit: int,
    before_cursor: str = "",
    participant_ref: str = "",
    include_commands: bool = False,
) -> tuple[list[MessageView], str]:
    bounded_limit = min(50, max(1, int(limit)))
    filters = [
        *_scope_filters(AmbientMessage, scope),
        AmbientMessage.deleted_at.is_(None),
    ]
    if not include_commands:
        filters.append(AmbientMessage.is_command.is_(False))
    if participant_ref:
        filters.append(AmbientMessage.participant_ref == participant_ref)
    if before_cursor:
        try:
            cursor_id = int(before_cursor)
        except ValueError as exc:
            raise ValueError("Invalid message cursor") from exc
        filters.append(AmbientMessage.id < cursor_id)
    async with get_session() as session:
        rows = list(
            (
                await session.execute(
                    select(AmbientMessage).where(*filters).order_by(AmbientMessage.id.desc()).limit(bounded_limit + 1)
                )
            )
            .scalars()
            .all()
        )
    has_more = len(rows) > bounded_limit
    selected = rows[:bounded_limit]
    selected.reverse()
    message_cursors = {row.message_id: str(row.id) for row in selected}
    now = datetime.now(timezone.utc)
    views: list[MessageView] = [
        {
            "cursor": str(row.id),
            "participant_ref": "bot" if row.is_bot else row.participant_ref,
            "display_name": "bot" if row.is_bot else row.display_name,
            "content": row.content or "[Message unavailable]",
            "created_at": _utc_iso(row.created_at),
            "minutes_ago": _minutes_ago(row.created_at, now),
            "directed_to_bot": row.directed_to_bot,
            "is_bot": row.is_bot,
            "reply_to_cursor": message_cursors.get(row.reply_to_message_id, ""),
        }
        for row in selected
    ]
    next_cursor = str(selected[0].id) if has_more and selected else ""
    return views, next_cursor


async def get_ambient_context(
    scope: PerceptionScope,
    *,
    max_messages: int,
    max_chars: int,
    exclude_message_id: str = "",
) -> list[dict[str, object]]:
    message_limit = min(20, max(0, int(max_messages)))
    char_limit = min(12000, max(0, int(max_chars)))
    if message_limit == 0 or char_limit == 0:
        return []
    filters = [
        *_scope_filters(AmbientMessage, scope),
        AmbientMessage.deleted_at.is_(None),
        AmbientMessage.directed_to_bot.is_(False),
        AmbientMessage.is_command.is_(False),
        AmbientMessage.is_bot.is_(False),
    ]
    if exclude_message_id:
        filters.append(AmbientMessage.message_id != exclude_message_id)
    async with get_session() as session:
        rows = list(
            (
                await session.execute(
                    select(AmbientMessage).where(*filters).order_by(AmbientMessage.id.desc()).limit(message_limit * 3)
                )
            )
            .scalars()
            .all()
        )
    now = datetime.now(timezone.utc)
    selected: list[dict[str, object]] = []
    used = 2
    for row in rows:
        item: dict[str, object] = {
            "participant_ref": row.participant_ref,
            "display_name": row.display_name,
            "content": row.content or "[Message unavailable]",
            "minutes_ago": _minutes_ago(row.created_at, now),
            "replies_to_recent_message": bool(row.reply_to_message_id),
        }
        size = len(json.dumps(item, ensure_ascii=False, separators=(",", ":"))) + (1 if selected else 0)
        if used + size > char_limit:
            continue
        selected.append(item)
        used += size
        if len(selected) >= message_limit:
            break
    selected.reverse()
    return selected
