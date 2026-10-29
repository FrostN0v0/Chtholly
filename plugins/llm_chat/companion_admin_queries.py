"""Read-only SQLite projections for bounded, scoped companion reflection history."""

from __future__ import annotations

import json
import base64
from typing import Any, cast
import binascii
from datetime import UTC, datetime
from collections.abc import Mapping, Sequence

from sqlalchemy import JSON, or_, and_, case, func, select, tuple_, type_coerce
from entari_plugin_database import get_session

from .models import AgentTurn, ChatScope, AgentEvent, ContextSession
from .companion.types import CompanionCorruptState
from .companion.models import CompanionMember, CompanionReview, CompanionContact, CompanionChatEvidence
from .core.personality import validate_persona_key
from .companion.evidence import has_member_input
from .session_inspection import summarize_usage, project_model_calls
from .companion.persistence import receipts
from .companion_admin_projection import (
    REVIEW_STATUSES,
    CompanionAdminError,
    utc,
    json_object,
    contact_view,
    bounded_value,
    reflection_records,
)

_MAX_EVENTS = 64
_MAX_CONTACTS = 20
_AUDIT_CHARS = 100000
_REFLECTION_EVENTS = frozenset({"companion_state", "companion_reflection", "companion_contact_decision"})
_MODEL_EVENTS = frozenset({"model_request", "model_response", "model_attempt", "context_snapshot"})


async def scoped_scope(db: Any, scope_ref: str) -> ChatScope:
    if not isinstance(scope_ref, str) or not scope_ref.strip() or len(scope_ref) > 200:
        raise CompanionAdminError("Unknown scope_ref", code="scope_not_found", status=404)
    scope = await db.scalar(select(ChatScope).where(ChatScope.scope_ref == scope_ref.strip()))
    if scope is None:
        raise CompanionAdminError("Unknown scope_ref", code="scope_not_found", status=404)
    return scope


def _cursor(row: CompanionReview, scope_ref: str, persona_key: str, status: str) -> str:
    payload = {
        "scope": scope_ref,
        "persona": persona_key,
        "status": status,
        "at": utc(row.started_at),
        "ref": row.lease_token,
    }
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")


def _cursor_values(cursor: str, scope_ref: str, persona_key: str, status: str) -> tuple[datetime, str]:
    try:
        if not isinstance(cursor, str) or len(cursor) > 1500:
            raise ValueError
        payload = json.loads(
            base64.b64decode(cursor.encode("ascii") + b"=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        )
        if not isinstance(payload, dict) or (payload.get("scope"), payload.get("persona"), payload.get("status")) != (
            scope_ref,
            persona_key,
            status,
        ):
            raise ValueError
        stamp, ref = payload["at"], payload["ref"]
        if not isinstance(stamp, str) or not isinstance(ref, str) or not 1 <= len(ref) <= 200:
            raise ValueError
        moment = datetime.fromisoformat(stamp)
        if moment.tzinfo is None:
            raise ValueError
        return moment.astimezone(UTC).replace(tzinfo=None), ref
    except (ValueError, TypeError, KeyError, binascii.Error, UnicodeError, RecursionError):
        raise CompanionAdminError("Invalid review cursor", code="invalid_cursor") from None


async def _native_turns(
    db: Any, scope_id: int, reviews: Sequence[CompanionReview]
) -> dict[str, tuple[AgentTurn, ContextSession]]:
    if not reviews:
        return {}
    lease = func.json_extract(
        case((func.json_valid(AgentEvent.payload_json) == 1, AgentEvent.payload_json), else_="{}"), "$.lease_token"
    )
    rows = (
        await db.execute(
            select(lease, AgentTurn, ContextSession)
            .select_from(AgentEvent)
            .join(AgentTurn, AgentTurn.id == AgentEvent.turn_id)
            .join(ContextSession, ContextSession.id == AgentTurn.session_id)
            .where(
                lease.in_([row.lease_token for row in reviews]),
                AgentEvent.event_type == "companion_reflection",
                AgentEvent.status == "started",
                AgentEvent.model_visible.is_(False),
                ContextSession.scope_id == scope_id,
                ContextSession.purpose == "companion_reflection",
                AgentTurn.trigger_kind == "reflection",
            )
            .order_by(AgentTurn.id.asc(), AgentEvent.sequence.asc())
            .limit(len(reviews) * 2)
        )
    ).all()
    result = {}
    for ref, turn, session in rows:
        # Ambiguous duplicate native audit ownership is not a safe navigation target.
        if ref in result:
            result[ref] = None
        else:
            result[ref] = (turn, session)
    return {ref: native for ref, native in result.items() if native is not None}


async def _audits(db: Any, turn_ids: Sequence[int]) -> tuple[dict[int, list[AgentEvent]], set[int]]:
    """Project metadata in SQL; never transfer provider HTTP/messages bodies for list metrics."""
    if not turn_ids:
        return {}, set()
    payload = type_coerce(
        case((func.json_valid(AgentEvent.payload_json) == 1, AgentEvent.payload_json), else_="{}"), JSON
    )
    ranked = (
        select(
            AgentEvent.id,
            func.row_number().over(partition_by=AgentEvent.turn_id, order_by=AgentEvent.sequence.asc()).label("rank"),
        )
        .where(AgentEvent.turn_id.in_(turn_ids), AgentEvent.event_type.in_(_REFLECTION_EVENTS | _MODEL_EVENTS))
        .subquery()
    )
    rows = (
        await db.execute(
            select(
                AgentEvent.turn_id,
                AgentEvent.event_ref,
                AgentEvent.sequence,
                AgentEvent.attempt,
                AgentEvent.event_type,
                AgentEvent.status,
                AgentEvent.duration_ms,
                AgentEvent.model_visible,
                case(
                    (
                        AgentEvent.event_type.in_(_REFLECTION_EVENTS),
                        func.substr(AgentEvent.payload_json, 1, _AUDIT_CHARS + 1),
                    )
                ),
                payload["request_id"].as_string(),
                payload["model"].as_string(),
                payload["usage"],
                payload["metrics"],
                payload["capture_status"].as_string(),
                ranked.c.rank,
            )
            .join(ranked, ranked.c.id == AgentEvent.id)
            .where(ranked.c.rank <= _MAX_EVENTS + 1)
            .order_by(AgentEvent.turn_id.asc(), AgentEvent.sequence.asc())
        )
    ).all()
    grouped: dict[int, list[AgentEvent]] = {turn_id: [] for turn_id in turn_ids}
    truncated: set[int] = set()
    for (
        turn_id,
        ref,
        sequence,
        attempt,
        kind,
        status,
        duration,
        visible,
        raw,
        request_id,
        model,
        usage,
        metrics,
        capture,
        rank,
    ) in rows:
        if rank > _MAX_EVENTS:
            truncated.add(turn_id)
            continue
        if raw is not None:
            if len(raw) > _AUDIT_CHARS:
                truncated.add(turn_id)
                data = {"capture_status": "overflow"}
            else:
                data = json_object(raw)
                if data is None:
                    data = {"capture_status": "invalid"}
        else:
            data = {
                "request_id": request_id,
                "model": model,
                "usage": usage,
                "metrics": metrics,
                "capture_status": capture or "not_recorded",
            }
        grouped[turn_id].append(
            AgentEvent(
                turn_id=turn_id,
                event_ref=ref,
                sequence=sequence,
                attempt=attempt,
                event_type=kind,
                status=status,
                duration_ms=duration,
                model_visible=visible,
                payload_json=json.dumps(data),
            )
        )
    return grouped, truncated


def _native_link(native: tuple[AgentTurn, ContextSession] | None) -> dict[str, object] | None:
    if native is None:
        return None
    turn, session = native
    return {"turn_ref": turn.turn_ref, "session_ref": session.session_ref, "sequence": turn.sequence}


def _model_projection(events: Sequence[AgentEvent], status: str) -> tuple[str | None, list[dict[str, object]]]:
    calls = []
    models = []
    for call in project_model_calls(events, turn_status=status):
        model = call["model"] if isinstance(call.get("model"), str) and call["model"] else None
        if model and model not in models:
            models.append(model)
        calls.append(
            {
                key: call[key]
                for key in (
                    "request_event_ref",
                    "response_event_ref",
                    "attempt",
                    "status",
                    "duration_ms",
                    "usage",
                    "capture_status",
                )
            }
            | {"model": model}
        )
    return ", ".join(models) if models else None, calls


async def _contacts(
    db: Any, scope_id: int, reviews: Sequence[CompanionReview]
) -> tuple[dict[str, list[CompanionContact]], set[str]]:
    grouped = {row.lease_token: [] for row in reviews}
    if not reviews:
        return grouped, set()
    ranked = (
        select(
            CompanionContact.attempt_ref,
            func.row_number()
            .over(
                partition_by=CompanionContact.origin_review_token,
                order_by=(CompanionContact.created_at.asc(), CompanionContact.attempt_ref.asc()),
            )
            .label("rank"),
        )
        .where(CompanionContact.scope_id == scope_id, CompanionContact.origin_review_token.in_(grouped))
        .subquery()
    )
    rows = (
        await db.execute(
            select(CompanionContact, ranked.c.rank)
            .join(ranked, ranked.c.attempt_ref == CompanionContact.attempt_ref)
            .where(ranked.c.rank <= _MAX_CONTACTS + 1)
            .order_by(CompanionContact.created_at.asc(), CompanionContact.attempt_ref.asc())
        )
    ).all()
    personas = {row.lease_token: row.persona_key for row in reviews}
    truncated = set()
    for contact, rank in rows:
        if contact.persona_key != personas[contact.origin_review_token]:
            continue
        if rank > _MAX_CONTACTS:
            truncated.add(contact.origin_review_token)
        else:
            grouped[contact.origin_review_token].append(contact)
    return grouped, truncated


async def _contact_links(db: Any, contacts: Sequence[CompanionContact]) -> dict[str, dict[str, object]]:
    ids = [row.attempt_ref for row in contacts if row.turn_id is not None]
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(CompanionContact.attempt_ref, AgentTurn.turn_ref, AgentTurn.sequence, ContextSession.session_ref)
            .select_from(CompanionContact)
            .join(AgentTurn, AgentTurn.id == CompanionContact.turn_id)
            .join(ContextSession, ContextSession.id == AgentTurn.session_id)
            .where(
                CompanionContact.attempt_ref.in_(ids),
                ContextSession.scope_id == CompanionContact.scope_id,
                ContextSession.purpose == "companion",
                AgentTurn.trigger_kind == "autonomous",
                AgentTurn.user_id == CompanionContact.user_id,
            )
        )
    ).all()
    return {
        ref: {"turn_ref": turn_ref, "sequence": sequence, "session_ref": session_ref}
        for ref, turn_ref, sequence, session_ref in rows
    }


def _contact_item(row: CompanionContact, links: Mapping[str, object], *, detail: bool = False) -> dict[str, object]:
    try:
        count = len(receipts(row))
    except CompanionCorruptState:
        count = None
    proposal = json_object(row.proposal_json)
    target = proposal.get("target", "member") if proposal is not None else None
    item: dict[str, object] = {
        "attempt_ref": row.attempt_ref,
        "status": row.status,
        "reason": row.reason[:1000],
        "member_ref": row.member_ref,
        "target": target if isinstance(target, str) and target in {"member", "channel"} else None,
        "display_name": row.display_name[:200],
        "created_at": utc(row.created_at),
        "finished_at": utc(row.finished_at),
        "confirmed_count": count,
        "native_turn": links.get(row.attempt_ref),
    }
    if detail:
        item.update(
            {
                "proposal": bounded_value(proposal)[0],
                "limits": bounded_value(json_object(row.limits_json))[0],
                "transport_started_at": utc(row.transport_started_at),
                "transport_inflight": row.transport_inflight,
                "previous_contact_at": utc(row.previous_contact_at),
                "mode_revision": row.mode_revision,
                "context_revision": row.context_revision,
                "member_version": row.member_version,
                "receipt_capture": "recorded" if count is not None else "invalid",
            }
        )
    return item


def _summary(
    row: CompanionReview,
    native: tuple[AgentTurn, ContextSession] | None,
    events: Sequence[AgentEvent],
    contacts: list[dict[str, object]],
) -> dict[str, object]:
    records = reflection_records(events, review_ref=row.lease_token)
    proposal = records["proposal"]
    state = records["state"]
    model, calls = _model_projection(events, native[0].status if native else row.status)
    committed = row.status == "completed" and row.committed_life_version is not None
    return {
        "review_ref": row.lease_token,
        "persona_key": row.persona_key,
        "status": row.status,
        "reason": row.reason[:1000],
        "started_at": utc(row.started_at),
        "finished_at": utc(row.finished_at),
        "mode_revision": row.mode_revision,
        "context_revision": row.context_revision,
        "life_version": row.life_version,
        "committed_life_version": row.committed_life_version,
        "contact_proposed": proposal.get("contact") is not None
        if isinstance(proposal, dict) and "contact" in proposal
        else None,
        "contact": contact_view(proposal.get("contact"), state, contacts=contacts)
        if isinstance(proposal, dict)
        else None,
        "committed_contact": contact_view(json_object(row.contact_proposal_json), state, contacts=contacts)
        if committed
        else None,
        "committed": committed,
        "decision": bounded_value(records["decision"], maximum=8000)[0],
        "transaction_committed": committed,
        "native_turn": _native_link(native),
        "model": model,
        "usage": summarize_usage(events),
        "model_calls": calls,
        "duration_ms": max(0, int((row.finished_at - row.started_at).total_seconds() * 1000))
        if row.finished_at
        else None,
        "contacts": contacts,
    }


async def companion_reviews(
    scope_ref: str,
    *,
    limit: int = 30,
    cursor: str = "",
    persona_key: str = "",
    status: str = "",
    session_factory: Any = None,
) -> dict[str, object]:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise CompanionAdminError("Invalid review limit")
    status = "" if status == "all" else status
    if status not in REVIEW_STATUSES | {""}:
        raise CompanionAdminError("Invalid review status")
    if persona_key:
        try:
            validate_persona_key(persona_key)
            if len(persona_key) > 200:
                raise ValueError
        except ValueError:
            raise CompanionAdminError("Invalid persona_key") from None
    async with (session_factory or get_session)() as db:
        scope = await scoped_scope(db, scope_ref)
        query = select(CompanionReview).where(CompanionReview.scope_id == scope.id)
        if persona_key:
            query = query.where(CompanionReview.persona_key == persona_key)
        if status:
            query = query.where(CompanionReview.status == status)
        if cursor:
            moment, ref = _cursor_values(cursor, scope.scope_ref, persona_key, status)
            cursor_row = await db.scalar(
                query.where(CompanionReview.lease_token == ref, CompanionReview.started_at == moment)
            )
            if cursor_row is None:
                raise CompanionAdminError("Invalid review cursor", code="invalid_cursor")
            query = query.where(
                or_(
                    CompanionReview.started_at < moment,
                    and_(CompanionReview.started_at == moment, CompanionReview.lease_token < ref),
                )
            )
        rows = list(
            (
                await db.scalars(
                    query.order_by(CompanionReview.started_at.desc(), CompanionReview.lease_token.desc()).limit(
                        limit + 1
                    )
                )
            ).all()
        )
        more, rows = len(rows) > limit, rows[:limit]
        native = await _native_turns(db, scope.id, rows)
        audit, audit_truncated = await _audits(db, [turn.id for turn, _ in native.values()])
        contacts, contacts_truncated = await _contacts(db, scope.id, rows)
        links = await _contact_links(db, [contact for rows_ in contacts.values() for contact in rows_])
        items = []
        for row in rows:
            linked = native.get(row.lease_token)
            events = audit.get(linked[0].id, []) if linked else []
            item = _summary(
                row, linked, events, [_contact_item(contact, links) for contact in contacts[row.lease_token]]
            )
            item["audit_truncated"] = bool(linked and linked[0].id in audit_truncated)
            item["contacts_truncated"] = row.lease_token in contacts_truncated
            items.append(item)
        return {
            "items": items,
            "next_cursor": _cursor(rows[-1], scope.scope_ref, persona_key, status) if more and rows else None,
        }


def _evidence_pairs(state: object, proposal: object, contacts: Sequence[CompanionContact]) -> set[tuple[str, int]]:
    pairs: set[tuple[str, int]] = set()

    def collect(item: object, field: str = "evidence_turn_ids") -> None:
        if not isinstance(item, Mapping) or not isinstance(item.get("member_ref"), str):
            return
        ids = item.get(field)
        if isinstance(ids, list):
            pairs.update(
                (item["member_ref"], value) for value in ids[:100] if type(value) is int and 0 < value <= 2**63 - 1
            )

    if isinstance(state, Mapping) and isinstance(state.get("members"), list):
        for member in state["members"][:100]:
            collect(member, "known_evidence_turn_ids")
            if isinstance(member, Mapping) and isinstance(member.get("attention"), list):
                for attention in member["attention"][:24]:
                    collect(attention)
            if isinstance(member, Mapping) and isinstance(member.get("episodes"), list):
                for episode in member["episodes"][:32]:
                    if isinstance(episode, Mapping):
                        collect({"member_ref": member.get("member_ref"), "evidence_turn_ids": [episode.get("turn_id")]})
    if isinstance(proposal, Mapping):
        collect(proposal.get("contact"))
        if isinstance(proposal.get("attention"), list):
            for attention in proposal["attention"][:24]:
                collect(attention)
    for contact in contacts:
        collect(json_object(contact.proposal_json))
    return pairs


async def _evidence_links(
    db: Any,
    review: CompanionReview,
    pairs: set[tuple[str, int]],
    *,
    source_session_id: int | None = None,
) -> list[dict[str, object]]:
    if not pairs:
        return []
    rows = (
        await db.execute(
            select(
                CompanionChatEvidence.member_ref,
                AgentTurn,
                ContextSession.session_ref,
                CompanionMember,
            )
            .select_from(CompanionChatEvidence)
            .join(AgentTurn, AgentTurn.id == CompanionChatEvidence.turn_id)
            .join(ContextSession, ContextSession.id == AgentTurn.session_id)
            .join(
                CompanionMember,
                and_(
                    CompanionMember.scope_id == CompanionChatEvidence.scope_id,
                    CompanionMember.persona_key == CompanionChatEvidence.persona_key,
                    CompanionMember.member_ref == CompanionChatEvidence.member_ref,
                ),
            )
            .where(
                CompanionChatEvidence.scope_id == review.scope_id,
                CompanionChatEvidence.persona_key == review.persona_key,
                tuple_(CompanionChatEvidence.member_ref, CompanionChatEvidence.turn_id).in_(sorted(pairs)),
                ContextSession.id == source_session_id if source_session_id is not None else True,
                AgentTurn.finished_at <= review.started_at if source_session_id is not None else True,
                ContextSession.scope_id == review.scope_id,
                or_(
                    and_(ContextSession.purpose == "chat", AgentTurn.trigger_kind == "message"),
                    and_(ContextSession.purpose == "companion", AgentTurn.trigger_kind == "autonomous"),
                ),
                AgentTurn.status.in_(("completed", "silent", "declined")),
                AgentTurn.finished_at.is_not(None),
            )
            .order_by(AgentTurn.id.asc())
            .limit(1001)
        )
    ).all()
    links = []
    scope = await db.get(ChatScope, review.scope_id)
    if scope is None:
        return []
    for member_ref, turn, session_ref, member in rows:
        if (member_ref, turn.id) not in pairs or not await has_member_input(
            db,
            turn,
            user_id=member.user_id,
            platform_user_id=member.platform_user_id,
            channel_id=scope.channel_id,
        ):
            continue
        links.append(
            {
                "member_ref": member_ref,
                "turn_id": turn.id,
                "turn_ref": turn.turn_ref,
                "sequence": turn.sequence,
                "session_ref": session_ref,
            }
        )
    return links[:1000]


async def companion_review_detail(scope_ref: str, review_ref: str, *, session_factory: Any = None) -> dict[str, object]:
    if not isinstance(review_ref, str) or not 1 <= len(review_ref) <= 200:
        raise CompanionAdminError("Unknown review_ref", code="review_not_found", status=404)
    async with (session_factory or get_session)() as db:
        scope = await scoped_scope(db, scope_ref)
        row = await db.scalar(
            select(CompanionReview).where(
                CompanionReview.scope_id == scope.id, CompanionReview.lease_token == review_ref
            )
        )
        if row is None:
            raise CompanionAdminError("Unknown review_ref", code="review_not_found", status=404)
        native = (await _native_turns(db, scope.id, [row])).get(row.lease_token)
        grouped, audit_truncated = await _audits(db, [native[0].id] if native else [])
        events = grouped.get(native[0].id, []) if native else []
        grouped_contacts, contact_truncated = await _contacts(db, scope.id, [row])
        contacts = grouped_contacts[row.lease_token]
        links = await _contact_links(db, contacts)
        item = _summary(row, native, events, [_contact_item(contact, links, detail=True) for contact in contacts])
        records = reflection_records(events, review_ref=row.lease_token)
        state, proposal = records["state"], records["proposal"]
        state_view, state_truncated = bounded_value(state)
        proposal_view, proposal_truncated = bounded_value(proposal)
        attention_before = None
        if isinstance(state_view, Mapping) and isinstance(state_view.get("members"), list):
            attention_before = [
                {"member_ref": member.get("member_ref"), "attention": member.get("attention")}
                for member in state_view["members"]
                if isinstance(member, Mapping)
            ]
        item.update(
            {
                "state": state_view,
                "proposal": proposal_view,
                "events": records["events"],
                "historical": True,
                "evidence_turns": await _evidence_links(db, row, _evidence_pairs(state, proposal, contacts)),
                "changes": {
                    "life_before": state_view.get("life") if isinstance(state_view, Mapping) else None,
                    "life_proposed": proposal_view.get("life") if isinstance(proposal_view, Mapping) else None,
                    "attention_before": attention_before,
                    "attention_proposed": proposal_view.get("attention")
                    if isinstance(proposal_view, Mapping)
                    else None,
                    # Generated-only, failed and cancelled proposals cannot claim a transaction commit.
                    "committed": item["committed"] and proposal is not None,
                },
                "capture": {
                    **cast(Mapping[str, object], records["capture"]),
                    "truncated": {"state": state_truncated, "proposal": proposal_truncated},
                },
                "audit_truncated": bool(native and native[0].id in audit_truncated),
                "contacts_truncated": row.lease_token in contact_truncated,
            }
        )
        return item
