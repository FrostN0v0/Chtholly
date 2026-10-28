"""Host-attributed member evidence shared by storage, reflection and admin readers."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AgentTurn, AgentEvent
from ..relationships.identity import resolve_relationship_owner
from ..scheduled_chat_context import _own_input_text


def _host_input_key(payload: Mapping[str, object]) -> tuple[str, str, str] | None:
    author = payload.get("host_author")
    if not isinstance(author, Mapping) or type(author.get("version")) is not int or author.get("version") != 1:
        return None
    subject, speaker, source = author.get("user_id"), author.get("speaker_id"), author.get("source_message_id")
    if not all(isinstance(value, str) for value in (subject, speaker, source)) or not subject or not speaker:
        return None
    return subject, speaker, source


def confirmed_engaged_inputs(events: Sequence[AgentEvent]) -> frozenset[tuple[str, str, str]]:
    """Validate exact finalized sources against real authors and confirmed receipt evidence."""
    authors: set[tuple[str, str, str]] = set()
    participation = []
    confirmed = False
    for event in events:
        payload = json.loads(event.payload_json)
        if not isinstance(payload, Mapping):
            continue
        if event.event_type == "user_input" and event.model_visible:
            key = _host_input_key(payload)
            if key is not None:
                authors.add(key)
        elif event.event_type == "companion_participation" and not event.model_visible:
            if type(payload.get("version")) is int and payload.get("version") == 1 and payload.get("confirmed") is True:
                participants = payload.get("participants")
                if isinstance(participants, list) and len(participants) <= 64:
                    participation.extend(participants)
        elif event.event_type == "assistant_output" and event.model_visible and event.status == "confirmed":
            confirmed = True
        elif event.event_type == "response_decision" and not event.model_visible:
            delivery = payload.get("actual_delivery")
            count = delivery.get("confirmed_deliveries") if isinstance(delivery, Mapping) else None
            if type(count) is int and count > 0:
                confirmed = True
    if not confirmed:
        return frozenset()
    sources = set()
    for item in participation:
        if not isinstance(item, Mapping) or not isinstance(item.get("source_message_ids"), list):
            continue
        subject, speaker = item.get("user_id"), item.get("speaker_id")
        if not isinstance(subject, str) or not isinstance(speaker, str):
            continue
        selected = item["source_message_ids"]
        if selected and all(isinstance(source, str) and (subject, speaker, source) in authors for source in selected):
            sources.update((subject, speaker, source) for source in selected)
    return frozenset(sources)


def attributed_input_owner(
    payload: Mapping[str, object],
    fallback_user_id: str,
    *,
    engaged_inputs: frozenset[tuple[str, str, str]] = frozenset(),
) -> str | None:
    """Legacy turns have one owner; companion inputs have an exact eligible host author."""
    if "host_author" not in payload:
        return fallback_user_id or None
    key = _host_input_key(payload)
    if key is None:
        return None
    author = payload["host_author"]
    directed = author.get("directed") is True and author.get("evidence_eligible") is True
    return key[0] if directed or key in engaged_inputs else None


def eligible_member_inputs(
    events: Sequence[AgentEvent], fallback_user_id: str
) -> list[tuple[Mapping[str, object], str]]:
    """Never let an unattributed sibling become root evidence in a companion turn."""
    parsed = [(event, json.loads(event.payload_json)) for event in events if event.event_type == "user_input"]
    companion = any(isinstance(payload, Mapping) and "host_author" in payload for _, payload in parsed)
    engaged = confirmed_engaged_inputs(events)
    result = []
    for event, payload in parsed:
        if not event.model_visible or not isinstance(payload, Mapping) or (companion and "host_author" not in payload):
            continue
        owner = attributed_input_owner(payload, fallback_user_id, engaged_inputs=engaged)
        if owner is not None:
            result.append((payload, owner))
    return result


def member_input_text(payload: Mapping[str, object]) -> str | None:
    """Use captured own words; older native events retain their structured-text reader."""
    if "host_author" in payload and "own_text" in payload:
        own_text = payload["own_text"]
        return own_text if isinstance(own_text, str) else None
    return _own_input_text(payload)


async def member_input_events(db: AsyncSession, turn_id: int) -> list[AgentEvent]:
    return list(
        (
            await db.scalars(
                select(AgentEvent)
                .where(
                    AgentEvent.turn_id == turn_id,
                    AgentEvent.event_type.in_(
                        ("user_input", "companion_participation", "assistant_output", "response_decision")
                    ),
                )
                .order_by(AgentEvent.sequence)
            )
        ).all()
    )


async def has_member_input(
    db: AsyncSession,
    turn: AgentTurn,
    *,
    user_id: str,
    platform_user_id: str,
    channel_id: str,
) -> bool:
    events = await member_input_events(db, turn.id)
    inputs = [event for event in events if event.event_type == "user_input"]
    owners = {await resolve_relationship_owner(db, value, channel_id) for value in (user_id, platform_user_id) if value}
    if not inputs:
        # Legacy imports can lack events, but a host-only opening is never human evidence.
        return (
            bool(turn.user_id)
            and turn.trigger_kind == "message"
            and (await resolve_relationship_owner(db, turn.user_id, channel_id) in owners)
        )
    for payload, subject in eligible_member_inputs(events, turn.user_id if turn.trigger_kind == "message" else ""):
        if (
            member_input_text(payload) is not None
            and await resolve_relationship_owner(db, subject, channel_id) in owners
        ):
            return True
    return False
