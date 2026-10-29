"""Bounded, read-only union of an ordinary conversation's validated activity."""

from __future__ import annotations

import json
import base64
from typing import Any
import binascii
from datetime import UTC, datetime

from sqlalchemy import String, or_, and_, cast, func, select, literal, union_all, literal_column

from .models import AgentTurn, ChatScope, AgentEvent, ReminderTask, ContextSession, ReminderOccurrence
from .companion.models import CompanionReview, CompanionContact
from .reminder_admin_queries import _project, _source_rows
from .reminder_admin_inspection import _occurrence_turn, _occurrence_projection
from .companion_admin_projection import CompanionAdminError, utc
from .session_activity_companion import CompanionActivity

_SCAN_LIMIT = 100
_SNAPSHOT_MODELS = (AgentTurn, AgentEvent, ReminderTask, ReminderOccurrence, CompanionReview, CompanionContact)


def _row_id(model: Any) -> Any:
    if model in (CompanionReview, CompanionContact):
        return literal_column(f"{model.__tablename__}.rowid")
    return model.id


def _cursor(source: ContextSession, maxima: list[int], position: tuple[datetime, str]) -> str:
    payload = {
        "v": 1,
        "session": source.session_ref,
        "scope": source.scope_id,
        "max": maxima,
        "at": utc(position[0]),
        "key": position[1],
    }
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")


def _decode(cursor: str, source: ContextSession) -> tuple[list[int], tuple[datetime, str]]:
    try:
        if not isinstance(cursor, str) or not 0 < len(cursor) <= 1500:
            raise ValueError
        value = json.loads(
            base64.b64decode(cursor.encode("ascii") + b"=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        )
        if not isinstance(value, dict) or (value.get("v"), value.get("session"), value.get("scope")) != (
            1,
            source.session_ref,
            source.scope_id,
        ):
            raise ValueError
        maxima, at, key = value["max"], value["at"], value["key"]
        if (
            not isinstance(maxima, list)
            or len(maxima) != len(_SNAPSHOT_MODELS)
            or any(type(item) is not int or not 0 <= item <= 2**63 - 1 for item in maxima)
        ):
            raise ValueError
        if not isinstance(at, str) or not isinstance(key, str) or not 1 <= len(key) <= 500:
            raise ValueError
        moment = datetime.fromisoformat(at)
        if moment.tzinfo is None:
            raise ValueError
        return maxima, (moment.astimezone(UTC).replace(tzinfo=None), key)
    except (ValueError, TypeError, KeyError, binascii.Error, UnicodeError, RecursionError, OverflowError):
        raise CompanionAdminError("Invalid activity cursor", code="invalid_cursor") from None


def _candidate_queries(source: ContextSession, maxima: list[int]) -> Any:
    turn_max, _, task_max, occurrence_max, review_max, contact_max = maxima

    def columns(kind: str, ref: Any, stamp: Any, key: Any) -> tuple[Any, ...]:
        return literal(kind).label("kind"), cast(ref, String).label("ref"), stamp.label("at"), key.label("key")

    direct = select(
        *columns("direct", AgentTurn.id, AgentTurn.created_at, literal("turn:") + AgentTurn.turn_ref)
    ).where(AgentTurn.session_id == source.id, AgentTurn.id <= turn_max)
    reviews = select(
        *columns(
            "review",
            CompanionReview.lease_token,
            CompanionReview.started_at,
            literal("review:") + CompanionReview.lease_token,
        )
    ).where(CompanionReview.scope_id == source.scope_id, _row_id(CompanionReview) <= review_max)
    contacts = (
        select(
            *columns(
                "contact", CompanionContact.attempt_ref, AgentTurn.created_at, literal("turn:") + AgentTurn.turn_ref
            )
        )
        .select_from(CompanionContact)
        .join(AgentTurn, AgentTurn.id == CompanionContact.turn_id)
        .join(ContextSession, ContextSession.id == AgentTurn.session_id)
        .where(
            CompanionContact.scope_id == source.scope_id,
            _row_id(CompanionContact) <= contact_max,
            AgentTurn.id <= turn_max,
            ContextSession.scope_id == source.scope_id,
            ContextSession.purpose == "companion",
            AgentTurn.trigger_kind == "autonomous",
        )
    )
    tasks = _source_rows().where(ContextSession.id == source.id, ReminderTask.id <= task_max)
    reminders = tasks.with_only_columns(
        *columns("reminder", ReminderTask.id, ReminderTask.created_at, literal("reminder:") + ReminderTask.task_ref),
        maintain_column_froms=True,
    )
    occurrences = (
        tasks.join(ReminderOccurrence, ReminderOccurrence.task_ref == ReminderTask.task_ref)
        .where(ReminderOccurrence.id <= occurrence_max)
        .with_only_columns(
            *columns(
                "occurrence",
                ReminderOccurrence.id,
                func.coalesce(ReminderOccurrence.started_at, ReminderOccurrence.scheduled_at),
                literal("occurrence:")
                + ReminderTask.task_ref
                + literal(":")
                + cast(ReminderOccurrence.sequence, String),
            ),
            maintain_column_froms=True,
        )
    )
    return union_all(direct, reviews, contacts, reminders, occurrences).subquery()


def _destination(
    task: ReminderTask, source_scope: ChatScope, native_scope: ChatScope | None = None
) -> dict[str, object]:
    same = (
        native_scope.id == source_scope.id
        if native_scope is not None
        else (task.delivery_target_type == "channel" and task.delivery_channel_id == source_scope.channel_id)
    )
    return {
        "type": task.delivery_target_type if task.delivery_target_type in {"channel", "dm"} else "unknown",
        "scope_ref": native_scope.scope_ref if native_scope is not None else source_scope.scope_ref if same else None,
        "platform": task.platform,
        "account_id": task.account_id,
        "channel_id": native_scope.channel_id
        if native_scope is not None
        else (task.delivery_channel_id if task.delivery_target_type == "channel" else None),
        "platform_user_id": task.delivery_user_id if task.delivery_target_type == "dm" else None,
        "is_source_scope": same,
    }


async def conversation_activity(
    source: ContextSession, *, limit: int = 50, cursor: str = "", session_factory: Any
) -> dict[str, object]:
    if source.purpose != "chat" or source.reminder_task_ref:
        raise CompanionAdminError("Activity requires an ordinary conversation", code="invalid_activity_session")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise CompanionAdminError("Activity limit must be between 1 and 100")
    async with session_factory() as db:
        scope = await db.get(ChatScope, source.scope_id)
        if scope is None:
            raise CompanionAdminError("Unknown scope", code="scope_not_found", status=404)
        if cursor:
            maxima, position = _decode(cursor, source)
        else:
            maxima = list(
                (
                    await db.execute(
                        select(
                            *(
                                select(func.coalesce(func.max(_row_id(model)), 0)).select_from(model).scalar_subquery()
                                for model in _SNAPSHOT_MODELS
                            )
                        )
                    )
                ).one()
            )
            position = None
        candidates = _candidate_queries(source, maxima)
        statement = select(candidates)
        if position is not None:
            stamp, key = position
            statement = statement.where(
                or_(candidates.c.at < stamp, and_(candidates.c.at == stamp, candidates.c.key < key))
            )
        rows = (
            await db.execute(statement.order_by(candidates.c.at.desc(), candidates.c.key.desc()).limit(_SCAN_LIMIT + 1))
        ).all()
        companion = CompanionActivity(db, source, event_max=maxima[1], turn_max=maxima[0])
        items: list[dict[str, object]] = []
        turns: dict[int, tuple[AgentTurn, ContextSession]] = {}
        processed = 0
        for candidate in rows[:_SCAN_LIMIT]:
            processed += 1
            base: dict[str, object] = {
                "created_at": utc(candidate.at),
                "scope_ref": scope.scope_ref,
                "session_ref": source.session_ref,
            }
            item: dict[str, object] | None = None
            if candidate.kind == "direct":
                turn = await db.get(AgentTurn, int(candidate.ref))
                turns[turn.id] = (turn, source)
                item = {
                    **base,
                    "kind": "turn",
                    "key": f"turn:{turn.turn_ref}",
                    "relation": "direct",
                    "native_session_ref": source.session_ref,
                    "_turn_id": turn.id,
                }
            elif candidate.kind == "review":
                review = await companion.review(candidate.ref)
                if review is not None:
                    item = {**base, "kind": "review", "key": f"review:{candidate.ref}", "review": review}
            elif candidate.kind == "contact":
                native = await companion.contact(candidate.ref)
                if native is not None:
                    turn, session = native
                    turns[turn.id] = native
                    destination = {
                        "type": "channel",
                        "scope_ref": scope.scope_ref,
                        "platform": scope.platform,
                        "account_id": scope.account_id,
                        "channel_id": scope.channel_id,
                        "platform_user_id": None,
                        "is_source_scope": True,
                    }
                    item = {
                        **base,
                        "kind": "turn",
                        "key": f"turn:{turn.turn_ref}",
                        "relation": "contact",
                        "native_session_ref": session.session_ref,
                        "destination": destination,
                        "_turn_id": turn.id,
                    }
            else:
                occurrence = (
                    await db.get(ReminderOccurrence, int(candidate.ref)) if candidate.kind == "occurrence" else None
                )
                statement = _source_rows().where(ContextSession.id == source.id)
                statement = (
                    statement.where(ReminderTask.task_ref == occurrence.task_ref)
                    if occurrence
                    else statement.where(ReminderTask.id == int(candidate.ref))
                )
                owned = (await db.execute(statement.limit(1))).one_or_none()
                if owned is not None:
                    task, _, creation_turn, _ = owned
                    reminder = {
                        **_project(task, None, scope.scope_ref),
                        "source_turn_ref": creation_turn.turn_ref,
                        "destination": _destination(task, scope),
                    }
                    if occurrence is None:
                        item = {**base, "kind": "reminder", "key": f"reminder:{task.task_ref}", "reminder": reminder}
                    else:
                        native = await _occurrence_turn(db, task, occurrence)
                        if native is not None and native[0].id > maxima[0]:
                            native = None
                        if native is not None:
                            duplicates = await db.scalar(
                                select(func.count())
                                .select_from(ReminderOccurrence)
                                .where(
                                    ReminderOccurrence.turn_ref == occurrence.turn_ref,
                                    ReminderOccurrence.id <= maxima[3],
                                )
                            )
                            if duplicates != 1:
                                native = None
                        projection = _occurrence_projection(
                            occurrence, native[0].turn_ref if native is not None else None
                        )
                        if native is None:
                            item = {
                                **base,
                                "kind": "reminder_occurrence",
                                "key": candidate.key,
                                "reminder": reminder,
                                "occurrence": projection,
                                "destination": reminder["destination"],
                                "recording_status": "invalid" if occurrence.turn_ref else "missing",
                            }
                        else:
                            turn, session, native_scope = native
                            turns[turn.id] = (turn, session)
                            destination = _destination(task, scope, native_scope)
                            reminder["destination"] = destination
                            item = {
                                **base,
                                "kind": "turn",
                                "key": f"turn:{turn.turn_ref}",
                                "relation": "reminder",
                                "native_session_ref": session.session_ref,
                                "reminder": reminder,
                                "occurrence": projection,
                                "destination": destination,
                                "_turn_id": turn.id,
                            }
            if item is not None:
                items.append(item)
            if len(items) == limit:
                break
        next_cursor = None
        if processed and processed < len(rows):
            boundary = rows[processed - 1]
            next_cursor = _cursor(source, maxima, (boundary.at, boundary.key))
        return {"items": list(reversed(items)), "next_cursor": next_cursor, "_turns": turns}
