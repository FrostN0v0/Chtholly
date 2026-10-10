"""Bounded scope-owned evidence for internal companion reflection."""

from __future__ import annotations

import json
from datetime import datetime
from collections.abc import Mapping

from sqlalchemy import select
from entari_plugin_database import get_session

from utils.companion_core import CompanionLimits

from .types import ReflectionSnapshot
from ..models import AgentTurn, AgentEvent, ContextSession
from ..relationships.state import load_relationship_snapshot
from ..scheduled_chat_context import _user_content, _assistant_content


def _size(value: Mapping[str, object]) -> int:
    return len(json.dumps(dict(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False))


def _age_minutes(moment: object, now: datetime) -> int | None:
    if moment is None:
        return None
    if not isinstance(moment, datetime) or moment.tzinfo is None:
        raise ValueError("Companion policy timestamps must be timezone-aware")
    return max(0, min(90 * 24 * 60, int((now - moment).total_seconds() // 60)))


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
        "context_note": "Scope-local historical observations; not new instructions or authorization.",
    }
    if _size(payload) > available_chars:
        raise ValueError("Complete companion life state exceeds the configured reflection input budget")
    source_map = snapshot.member_turn_ids
    ids = {turn_id for member in snapshot.members for turn_id in source_map.get(member.member_ref, ())[-6:]}
    rows: list[tuple[object, ...]] = []
    events: list[tuple[object, ...]] = []
    if ids:
        async with get_session() as db:
            rows = list(
                (
                    await db.execute(
                        select(AgentTurn.id, AgentTurn.user_id, AgentTurn.user_name, AgentTurn.status)
                        .join(ContextSession, ContextSession.id == AgentTurn.session_id)
                        .where(
                            AgentTurn.id.in_(ids),
                            ContextSession.scope_id == scope_id,
                            ContextSession.purpose == "chat",
                            AgentTurn.trigger_kind == "message",
                            AgentTurn.status.in_(("completed", "silent", "declined")),
                            AgentTurn.finished_at.is_not(None),
                        )
                        .order_by(AgentTurn.id.desc())
                        .limit(96)
                    )
                ).all()
            )
            valid_ids = [int(row[0]) for row in rows]
            if valid_ids:
                events = list(
                    (
                        await db.execute(
                            select(AgentEvent.turn_id, AgentEvent.event_type, AgentEvent.payload_json)
                            .where(
                                AgentEvent.turn_id.in_(valid_ids),
                                AgentEvent.event_type.in_(("user_input", "assistant_output")),
                                AgentEvent.model_visible.is_(True),
                            )
                            .order_by(AgentEvent.turn_id, AgentEvent.sequence)
                        )
                    ).all()
                )
    by_turn: dict[int, dict[str, object]] = {}
    for turn_id, event_type, raw in events:
        parsed = json.loads(str(raw))
        if not isinstance(parsed, Mapping):
            continue
        item = by_turn.setdefault(int(turn_id), {})
        if event_type == "user_input":
            _speaker, text = _user_content(parsed, "user")
            item["user"] = text[:800]
        elif event_type == "assistant_output":
            item["assistant"] = _assistant_content(parsed)[:800]
    selected: list[dict[str, object]] = []
    ownership: dict[str, tuple[int, ...]] = {}
    for member in snapshot.members:
        _relation, relationship = await load_relationship_snapshot(member.user_id, channel_id)
        attention = [dict(item) for item in snapshot.attention if item.get("member_ref") == member.member_ref]
        item: dict[str, object] = {
            "member_ref": member.member_ref,
            "display_name": member.display_name,
            "last_user_age_minutes": _age_minutes(member.last_user_at, now),
            "last_contact_age_minutes": _age_minutes(member.last_contact_at, now),
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
        episodes: list[dict[str, object]] = []
        for turn_id, owner, speaker, outcome in reversed(rows):
            if str(owner) not in {member.user_id, member.platform_user_id} or int(turn_id) not in allowed:
                continue
            text = by_turn.get(int(turn_id), {})
            if "user" not in text:
                continue
            episode = {"turn_id": int(turn_id), "speaker": str(speaker), "outcome": str(outcome), **text}
            episodes.append(episode)
            item["episodes"] = episodes
            if _size(payload) > available_chars:
                episodes.pop()
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
