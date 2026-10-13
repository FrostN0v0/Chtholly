"""Current companion state and actual daily counters, separate from historical reviews."""

from __future__ import annotations

from typing import Any
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from dataclasses import asdict

from sqlalchemy import case, func, select
from entari_plugin_database import get_session

from .models import ReminderPreference, ScopePersonaSelection
from .companion.models import (
    CompanionLife,
    CompanionMode,
    CompanionMember,
    CompanionReview,
    CompanionContact,
    CompanionAttention,
    CompanionLifeRecord,
    CompanionChatEvidence,
)
from .companion.context import companion_limits
from .companion_admin_queries import scoped_scope
from .companion_admin_projection import utc, json_object, bounded_value

_SHANGHAI = timezone(timedelta(hours=8))
_MAX_MEMBERS = 100
_MAX_ATTENTION = 24
_MAX_PERSONAS = 200


async def companion_overview(
    config: Any, scope_ref: str, *, now: datetime | None = None, session_factory: Any = None
) -> dict[str, object]:
    moment = now or datetime.now(timezone.utc)
    async with (session_factory or get_session)() as db:
        scope = await scoped_scope(db, scope_ref)
        mode = await db.get(CompanionMode, scope.id)
        selection = await db.get(ScopePersonaSelection, scope.id)
        # Missing mode is the real default normal policy, not an inferred historical mode.
        persona_key = mode.persona_key if mode else selection.persona_key if selection else ""
        life = await db.get(CompanionLife, (scope.id, persona_key)) if persona_key else None
        members = list(
            (
                await db.scalars(
                    select(CompanionMember)
                    .where(CompanionMember.scope_id == scope.id, CompanionMember.persona_key == persona_key)
                    .order_by(CompanionMember.last_user_at.desc(), CompanionMember.member_ref.asc())
                    .limit(_MAX_MEMBERS + 1)
                )
            ).all()
        )
        members_truncated, members = len(members) > _MAX_MEMBERS, members[:_MAX_MEMBERS]
        attention: dict[str, list[dict[str, object]]] = defaultdict(list)
        attention_truncated: set[str] = set()
        if members:
            ranked = (
                select(
                    CompanionAttention.scope_id,
                    CompanionAttention.persona_key,
                    CompanionAttention.member_ref,
                    CompanionAttention.key,
                    func.row_number()
                    .over(
                        partition_by=CompanionAttention.member_ref,
                        order_by=(CompanionAttention.updated_at.desc(), CompanionAttention.key.asc()),
                    )
                    .label("rank"),
                )
                .where(
                    CompanionAttention.scope_id == scope.id,
                    CompanionAttention.persona_key == persona_key,
                    CompanionAttention.member_ref.in_([member.member_ref for member in members]),
                )
                .subquery()
            )
            rows = (
                await db.execute(
                    select(CompanionAttention, ranked.c.rank)
                    .join(
                        ranked,
                        (ranked.c.scope_id == CompanionAttention.scope_id)
                        & (ranked.c.persona_key == CompanionAttention.persona_key)
                        & (ranked.c.member_ref == CompanionAttention.member_ref)
                        & (ranked.c.key == CompanionAttention.key),
                    )
                    .where(ranked.c.rank <= _MAX_ATTENTION + 1)
                    .order_by(
                        CompanionAttention.member_ref.asc(),
                        CompanionAttention.updated_at.desc(),
                        CompanionAttention.key.asc(),
                    )
                )
            ).all()
            for row, rank in rows:
                if rank > _MAX_ATTENTION:
                    attention_truncated.add(row.member_ref)
                    continue
                state = json_object(row.state_json)
                if state is not None and (state.get("member_ref"), state.get("key")) != (row.member_ref, row.key):
                    state = None
                state_view, truncated = bounded_value(state, maximum=2000)
                attention[row.member_ref].append(
                    {
                        "state": state_view,
                        "version": row.version,
                        "updated_at": utc(row.updated_at),
                        "capture": "recorded" if state is not None else "invalid",
                        "truncated": truncated,
                    }
                )
            preferences = (
                await db.scalars(
                    select(ReminderPreference).where(
                        ReminderPreference.account_id == scope.account_id,
                        ReminderPreference.unified_user_id.in_([member.user_id for member in members]),
                    )
                )
            ).all()
        else:
            preferences = []
        preference_by_user = {preference.unified_user_id: preference for preference in preferences}
        member_items = []
        for member in members:
            preference = preference_by_user.get(member.user_id)
            member_items.append(
                {
                    "member_ref": member.member_ref,
                    "display_name": member.display_name[:200],
                    "last_user_at": utc(member.last_user_at),
                    "last_contact_at": utc(member.last_contact_at),
                    "version": member.version,
                    "attention": attention[member.member_ref],
                    "attention_truncated": member.member_ref in attention_truncated,
                    "preference": {
                        "enabled": preference.enabled if preference is not None else True,
                        "recorded": preference is not None,
                        "source": "reminder_preference" if preference is not None else "default_allowed",
                        "updated_at": utc(preference.updated_at) if preference is not None else None,
                    },
                }
            )
        day = moment.astimezone(_SHANGHAI).date().isoformat()
        review_counts = (
            await db.execute(
                select(CompanionReview.status, func.count())
                .where(CompanionReview.scope_id == scope.id, CompanionReview.budget_day == day)
                .group_by(CompanionReview.status)
            )
        ).tuples().all()
        contact_counts = (
            await db.execute(
                select(CompanionContact.status, func.count())
                .where(CompanionContact.scope_id == scope.id, CompanionContact.budget_day == day)
                .group_by(CompanionContact.status)
            )
        ).tuples().all()
        safe_receipts = case(
            (func.json_valid(CompanionContact.confirmed_receipts_json) == 1, CompanionContact.confirmed_receipts_json),
            else_="null",
        )
        count, invalid_receipts = (
            await db.execute(
                select(
                    func.coalesce(func.sum(func.json_array_length(safe_receipts)), 0),
                    func.count().filter(func.coalesce(func.json_type(safe_receipts), "null") != "array"),
                ).where(CompanionContact.scope_id == scope.id, CompanionContact.budget_day == day)
            )
        ).one()
        persona_columns = [
            model.persona_key
            for model in (
                CompanionLife,
                CompanionLifeRecord,
                CompanionMember,
                CompanionChatEvidence,
                CompanionAttention,
                CompanionReview,
                CompanionContact,
                CompanionMode,
                ScopePersonaSelection,
            )
        ]
        persona_queries = [
            select(column.label("persona_key")).where(column.table.c.scope_id == scope.id) for column in persona_columns
        ]
        union = persona_queries[0].union(*persona_queries[1:]).subquery()
        personas = list(
            (
                await db.scalars(
                    select(union.c.persona_key)
                    .where(union.c.persona_key != "")
                    .order_by(union.c.persona_key.asc())
                    .limit(_MAX_PERSONAS + 1)
                )
            ).all()
        )
        current_life = None
        if life is not None:
            state = json_object(life.state_json)
            state_view, truncated = bounded_value(state, maximum=8000)
            current_life = {
                "persona_key": life.persona_key,
                "version": life.version,
                "updated_at": utc(life.updated_at),
                "state": state_view,
                "capture": "recorded" if state is not None else "invalid",
                "truncated": truncated,
            }
        return {
            "scope_ref": scope.scope_ref,
            "current": True,
            "mode": {
                "mode": mode.mode if mode else "normal",
                "persona_key": persona_key,
                "revision": mode.revision if mode else 0,
                "context_revision": mode.context_revision if mode else 0,
                "next_review_at": utc(mode.next_review_at) if mode else None,
                "updated_at": utc(mode.updated_at) if mode else None,
                "recorded": mode is not None,
            },
            "current_limits": asdict(companion_limits(config)),
            "life": current_life,
            "members": member_items,
            "members_truncated": members_truncated,
            "daily": {
                "day": day,
                "timezone": "Asia/Shanghai",
                "reviews": dict(review_counts),
                "contacts": dict(contact_counts),
                "confirmed_messages": int(count) if not invalid_receipts else None,
                "receipt_capture": "recorded" if not invalid_receipts else "invalid",
            },
            "personas": personas[:_MAX_PERSONAS],
            "personas_truncated": len(personas) > _MAX_PERSONAS,
        }
