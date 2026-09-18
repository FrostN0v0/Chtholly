"""Read-side bounded views for channel perception."""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta

from sqlalchemy import select
from entari_plugin_database import get_session

from .config import ChannelPerceptionConfig
from .models import AmbientMessage, ChannelParticipant
from .schemas import MessageView, ParticipantView, PerceptionScope, ParticipantSnapshot
from .message_store import MAX_RETENTION_DAYS, MAX_MESSAGES_PER_CHANNEL
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


def _readable_filters(scope: PerceptionScope, config: ChannelPerceptionConfig | None = None):
    config = config or ChannelPerceptionConfig()
    retained = (
        select(AmbientMessage.id)
        .where(*_scope_filters(AmbientMessage, scope))
        .order_by(AmbientMessage.created_at.desc(), AmbientMessage.id.desc())
        .limit(min(MAX_MESSAGES_PER_CHANNEL, max(1, int(config.max_messages_per_channel))))
    )
    cutoff = datetime.utcnow() - timedelta(days=min(MAX_RETENTION_DAYS, max(1, int(config.retention_days))))
    return (
        *_scope_filters(AmbientMessage, scope),
        AmbientMessage.deleted_at.is_(None),
        AmbientMessage.is_command.is_(False),
        AmbientMessage.created_at >= cutoff,
        AmbientMessage.id.in_(retained),
    )


def _native_mentions(row: AmbientMessage) -> list[dict[str, object]] | None:
    if row.mentions_json is None:
        return None
    try:
        value = json.loads(row.mentions_json)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, list) and all(isinstance(item, dict) for item in value) else None


async def _message_views(
    session, scope: PerceptionScope, rows: list[AmbientMessage], config: ChannelPerceptionConfig | None
) -> list[MessageView]:
    reply_ids = {row.reply_to_message_id for row in rows if row.reply_to_message_id}
    targets = (
        dict(
            (
                await session.execute(
                    select(AmbientMessage.message_id, AmbientMessage.id).where(
                        *_readable_filters(scope, config), AmbientMessage.message_id.in_(reply_ids)
                    )
                )
            ).all()
        )
        if reply_ids
        else {}
    )
    native = [_native_mentions(row) for row in rows]
    member_ids = {
        str(item.get("target_id", ""))
        for items in native
        if items is not None
        for item in items
        if item.get("kind") == "member" and item.get("target_id")
    }
    members = (
        dict(
            (
                await session.execute(
                    select(ChannelParticipant.platform_user_id, ChannelParticipant.public_ref).where(
                        *_scope_filters(ChannelParticipant, scope), ChannelParticipant.platform_user_id.in_(member_ids)
                    )
                )
            ).all()
        )
        if member_ids
        else {}
    )
    page_ids = {row.id for row in rows}
    now = datetime.now(timezone.utc)
    views: list[MessageView] = []
    for row, items in zip(rows, native):
        mentions: list[dict[str, object]] | None = None
        if items is not None:
            mentions = []
            for position, item in enumerate(items):
                kind = str(item.get("kind", "unknown"))
                target_id = str(item.get("target_id", ""))
                mention: dict[str, object] = {
                    "position": position,
                    "kind": kind,
                    "display_name": str(item.get("display_name", "")),
                }
                if kind == "member":
                    if target_id == scope.account_id:
                        mention.update(kind="bot", participant_ref="bot", status="available")
                    elif target_id in members:
                        mention.update(participant_ref=members[target_id], status="available")
                    else:
                        mention["status"] = "unknown"
                else:
                    mention["status"] = "available" if kind in {"all", "here", "role"} else "unknown"
                mentions.append(mention)
        target = targets.get(row.reply_to_message_id)
        views.append(
            {
                "cursor": str(row.id),
                "participant_ref": "bot" if row.is_bot else row.participant_ref,
                "display_name": "bot" if row.is_bot else row.display_name,
                "content": row.content or "[Message unavailable]",
                "image_count": row.image_count,
                "created_at": _utc_iso(row.created_at),
                "minutes_ago": _minutes_ago(row.created_at, now),
                "directed_to_bot": row.directed_to_bot,
                "is_bot": row.is_bot,
                "mentions": mentions,
                "reply_to_cursor": str(target) if target is not None else "",
                "reply_to_status": ("available" if target in page_ids else "outside_page")
                if target is not None
                else ("unavailable" if row.reply_to_message_id else "none"),
            }
        )
    return views


async def get_message_image_target(
    scope: PerceptionScope, cursor: str, *, config: ChannelPerceptionConfig | None = None
) -> tuple[str, int] | None:
    try:
        message_row_id = int(cursor)
    except ValueError:
        return None
    async with get_session() as session:
        row = (
            await session.execute(
                select(AmbientMessage.message_id, AmbientMessage.image_count).where(
                    *_readable_filters(scope, config),
                    AmbientMessage.id == message_row_id,
                )
            )
        ).one_or_none()
    if row is None:
        return None
    return str(row.message_id), max(0, int(row.image_count))


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
    config: ChannelPerceptionConfig | None = None,
) -> tuple[list[MessageView], str]:
    bounded_limit = min(50, max(1, int(limit)))
    filters = list(_readable_filters(scope, config))
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
        selected = list(reversed(rows[:bounded_limit]))
        views = await _message_views(session, scope, selected, config)
    next_cursor = str(selected[0].id) if has_more and selected else ""
    return views, next_cursor


async def get_exact_message(
    scope: PerceptionScope, cursor: str, *, config: ChannelPerceptionConfig | None = None
) -> MessageView | None:
    try:
        row_id = int(cursor)
    except ValueError:
        return None
    async with get_session() as session:
        row = (
            await session.execute(
                select(AmbientMessage).where(*_readable_filters(scope, config), AmbientMessage.id == row_id)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return (await _message_views(session, scope, [row], config))[0]


async def get_ambient_context(
    scope: PerceptionScope,
    *,
    max_messages: int,
    max_chars: int,
    exclude_message_id: str = "",
    config: ChannelPerceptionConfig | None = None,
) -> list[dict[str, object]]:
    message_limit = min(20, max(0, int(max_messages)))
    char_limit = min(12000, max(0, int(max_chars)))
    if message_limit == 0 or char_limit == 0:
        return []
    filters = [
        *_readable_filters(scope, config),
        AmbientMessage.directed_to_bot.is_(False),
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
        views = await _message_views(session, scope, rows, config)
    selected: list[dict[str, object]] = []
    used = 2
    for row, view in zip(rows, views):
        item: dict[str, object] = {
            "participant_ref": row.participant_ref,
            "display_name": row.display_name,
            "content": row.content or "[Message unavailable]",
            "cursor": str(row.id),
            "image_count": row.image_count,
            "minutes_ago": view["minutes_ago"],
            "mentions": view["mentions"],
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
