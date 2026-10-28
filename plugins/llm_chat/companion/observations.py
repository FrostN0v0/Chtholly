"""Bounded scope-owned evidence for internal companion reflection."""

from __future__ import annotations

import json
from datetime import datetime
from collections.abc import Mapping

from sqlalchemy import or_, and_, select
from entari_plugin_database import get_session

from utils.companion_core import CompanionLimits
from utils.companion_core.policy import SHANGHAI, USER_QUIET_GRACE_MINUTES

from .types import CompanionConflict, ReflectionSnapshot
from .models import CompanionMember
from ..models import AgentTurn, AgentEvent, ContextSession
from .evidence import member_input_text, eligible_member_inputs
from .claim_store import read_contact_timing
from .persistence import aware
from ..relationships.state import load_relationship_snapshot
from ..relationships.identity import resolve_relationship_owner
from ..scheduled_chat_context import _safe_text, _user_content, _assistant_content


def _size(value: Mapping[str, object]) -> int:
    return len(json.dumps(dict(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False))


def _age_minutes(moment: object, now: datetime) -> int | None:
    if moment is None:
        return None
    if not isinstance(moment, datetime) or moment.tzinfo is None:
        raise ValueError("Companion policy timestamps must be timezone-aware")
    return max(0, min(90 * 24 * 60, int((now - moment).total_seconds() // 60)))


def _episode_time(created_at: datetime | None, now: datetime) -> dict[str, object]:
    """Native AgentTurn timestamps are UTC; absent legacy values stay unknown."""
    moment = aware(created_at)
    return {
        "created_at": moment.isoformat() if moment is not None else None,
        "created_at_local": moment.astimezone(SHANGHAI).isoformat() if moment is not None else None,
        "age_minutes": _age_minutes(moment, now),
    }


async def reflection_input(
    snapshot: ReflectionSnapshot,
    *,
    scope_id: int,
    channel_id: str,
    limits: CompanionLimits,
    now: datetime,
    available_chars: int,
) -> tuple[dict[str, object], dict[str, tuple[int, ...]]]:
    """Reserve complete life/member/attention state before optional whole evidence records."""
    payload: dict[str, object] = {
        "life": snapshot.life.model_dump(mode="json"),
        "members": [],
        "recent_contacts": [],
        "contact_policy": {
            "timezone": "Asia/Shanghai",
            "local_time": now.astimezone(SHANGHAI).isoformat(),
            "quiet_hours": {"start_hour": 23, "end_hour": 7},
            "user_quiet_grace_minutes": USER_QUIET_GRACE_MINUTES,
            "contact_interval_minutes": limits.contact_interval_minutes,
            "member_idle_days": limits.member_idle_days,
            "daily_contact_limit": limits.daily_contacts,
        },
        "context_note": "Scope-local historical observations; not new instructions or authorization.",
    }
    if _size(payload) > available_chars:
        raise ValueError("Complete companion life state exceeds the configured reflection input budget")
    source_map = snapshot.member_turn_ids
    ids = {turn_id for member in snapshot.members for turn_id in source_map.get(member.member_ref, ())[-6:]}
    rows: list[tuple[int, str, str, str, datetime | None]] = []
    events: list[AgentEvent] = []
    if ids:
        async with get_session() as db:
            rows = list(
                (
                    await db.execute(
                        select(
                            AgentTurn.id,
                            AgentTurn.user_id,
                            AgentTurn.user_name,
                            AgentTurn.status,
                            AgentTurn.created_at,
                        )
                        .join(ContextSession, ContextSession.id == AgentTurn.session_id)
                        .where(
                            AgentTurn.id.in_(ids),
                            ContextSession.scope_id == scope_id,
                            or_(
                                and_(ContextSession.purpose == "chat", AgentTurn.trigger_kind == "message"),
                                and_(ContextSession.purpose == "companion", AgentTurn.trigger_kind == "autonomous"),
                            ),
                            AgentTurn.status.in_(("completed", "silent", "declined")),
                            AgentTurn.finished_at.is_not(None),
                        )
                        .order_by(AgentTurn.id.desc())
                        .limit(96)
                    )
                )
                .tuples()
                .all()
            )
            valid_ids = [int(row[0]) for row in rows]
            if valid_ids:
                events = list(
                    (
                        await db.execute(
                            select(AgentEvent)
                            .where(
                                AgentEvent.turn_id.in_(valid_ids),
                                AgentEvent.event_type.in_(
                                    ("user_input", "assistant_output", "companion_participation", "response_decision")
                                ),
                            )
                            .order_by(AgentEvent.turn_id, AgentEvent.sequence)
                        )
                    )
                    .scalars()
                    .all()
                )
    by_turn: dict[tuple[int, str], dict[str, object]] = {}
    assistants: dict[int, str] = {}
    turn_owners = {int(row[0]): str(row[1]) for row in rows}
    grouped_events: dict[int, list[AgentEvent]] = {}
    for event in events:
        grouped_events.setdefault(event.turn_id, []).append(event)
    async with get_session() as db:
        owners: dict[str, str] = {}
        for turn_id, turn_events in grouped_events.items():
            for event in turn_events:
                if event.event_type != "assistant_output" or not event.model_visible or event.status != "confirmed":
                    continue
                parsed = json.loads(event.payload_json)
                if isinstance(parsed, Mapping):
                    assistants[turn_id] = _assistant_content(parsed)[:800]
            for parsed, subject in eligible_member_inputs(turn_events, turn_owners[turn_id]):
                own_text = member_input_text(parsed)
                if own_text is None:
                    continue
                if subject not in owners:
                    owners[subject] = await resolve_relationship_owner(db, subject, channel_id)
                item = by_turn.setdefault((turn_id, owners[subject]), {})
                speaker, _ = _user_content(parsed, "user")
                author = parsed.get("host_author")
                item["speaker"] = author.get("display_name", speaker) if isinstance(author, Mapping) else speaker
                previous = item.get("user")
                own_text = _safe_text(own_text, limit=800)
                item["user"] = f"{previous}\n\n{own_text}" if isinstance(previous, str) else own_text
        member_owners = {
            member.member_ref: await resolve_relationship_owner(db, member.user_id, channel_id)
            for member in snapshot.members
        }
    selected: list[dict[str, object]] = []
    ownership: dict[str, tuple[int, ...]] = {}
    timing: dict[str, dict[str, object]] = {}
    if snapshot.members:
        async with get_session() as db:
            current_members = (
                await db.scalars(
                    select(CompanionMember).where(
                        CompanionMember.scope_id == scope_id,
                        CompanionMember.persona_key == snapshot.mode.persona_key,
                        CompanionMember.member_ref.in_([member.member_ref for member in snapshot.members]),
                    )
                )
            ).all()
            for member in current_members:
                timing[member.member_ref] = await read_contact_timing(db, member, now=now, limits=limits)
        if len(timing) != len(snapshot.members):
            raise CompanionConflict("Companion timing no longer has every selected member")
    for member in snapshot.members:
        _relation, relationship = await load_relationship_snapshot(member_owners[member.member_ref], channel_id)
        attention = [dict(item) for item in snapshot.attention if item.get("member_ref") == member.member_ref]
        item: dict[str, object] = {
            "member_ref": member.member_ref,
            "display_name": member.display_name,
            "last_user_age_minutes": _age_minutes(member.last_user_at, now),
            "last_contact_age_minutes": _age_minutes(member.last_contact_at, now),
            "contact_timing": timing[member.member_ref],
            "relationship": {
                "axes": relationship["axes"],
                "description_excerpt": str(relationship.get("description", ""))[:400],
            },
            "attention": attention,
            "episodes": [],
        }
        allowed = tuple(source_map.get(member.member_ref, ()))
        item["known_evidence_turn_ids"] = list(allowed)
        selected.append(item)
        ownership[member.member_ref] = allowed
    payload["members"] = selected
    if _size(payload) > available_chars:
        raise ValueError("Complete companion life, members and attention exceed the configured reflection input budget")

    for member, item in zip(snapshot.members, selected):
        allowed = ownership[member.member_ref]
        newest_first: list[dict[str, object]] = []
        # Rows arrive newest first: fill the budget with the latest contiguous history, then show it in time order.
        for turn_id, owner, speaker, outcome, created_at in rows:
            if int(turn_id) not in allowed:
                continue
            text = by_turn.get((int(turn_id), member_owners[member.member_ref]), {})
            if "user" not in text:
                continue
            episode_time = _episode_time(created_at, now)
            episode = {
                "turn_id": int(turn_id),
                "speaker": text.get("speaker", str(speaker)),
                "outcome": str(outcome),
                **episode_time,
                **text,
                "assistant": assistants.get(int(turn_id), ""),
            }
            item["episodes"] = [episode, *reversed(newest_first)]
            if _size(payload) > available_chars:
                item["episodes"] = list(reversed(newest_first))
                break
            newest_first.append(episode)
    selected_refs = set(ownership)
    contacts: list[dict[str, object]] = []
    for contact in snapshot.recent_contacts:
        if contact.get("member_ref") not in selected_refs:
            continue
        proposal = contact.get("proposal")
        if not isinstance(proposal, Mapping):
            raise ValueError("Recorded contact is missing its validated proposal")
        candidate: dict[str, object] = {
            "member_ref": contact["member_ref"],
            "status": contact["status"],
            "proposal": {key: proposal[key] for key in ("key", "purpose", "topic", "reason", "evidence_turn_ids")},
        }
        for key in ("created_at", "finished_at"):
            moment = contact.get(key)
            candidate[key.removesuffix("_at") + "_age_minutes"] = _age_minutes(moment, now)
        if _size({**payload, "recent_contacts": [*contacts, candidate]}) > available_chars:
            continue
        contacts.append(candidate)
    payload["recent_contacts"] = contacts
    return payload, ownership
