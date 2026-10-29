"""Historical ownership checks for companion entries in an ordinary conversation."""

from __future__ import annotations

from typing import Any
from collections.abc import Mapping

from sqlalchemy import JSON, case, func, select, type_coerce

from .models import AgentTurn, AgentEvent, ContextSession, SessionPersonaSnapshot
from .agent_events import load_event_payload
from .companion.models import CompanionMember, CompanionReview, CompanionContact
from .companion_admin_queries import _native_link, _native_turns, _contact_links, _evidence_links
from .companion_admin_projection import utc, json_object


async def _persona_matches(db: Any, session: ContextSession, persona_key: str) -> bool:
    snapshot = await db.get(SessionPersonaSnapshot, session.id)
    if snapshot is None:
        return True
    value = json_object(snapshot.snapshot_json)
    return value is not None and value.get("key") == persona_key


def _recorded_pairs(state: object, proposal: object) -> set[tuple[str, int]]:
    """Read all recorded evidence, rather than truncating a history association."""
    pairs: set[tuple[str, int]] = set()

    def collect(value: object, field: str = "evidence_turn_ids") -> None:
        if not isinstance(value, Mapping) or not isinstance(value.get("member_ref"), str):
            return
        ids = value.get(field)
        if isinstance(ids, list):
            pairs.update((value["member_ref"], item) for item in ids if type(item) is int and 0 < item <= 2**63 - 1)

    if isinstance(state, Mapping) and isinstance(state.get("members"), list):
        for member in state["members"]:
            collect(member, "known_evidence_turn_ids")
            if not isinstance(member, Mapping):
                continue
            if isinstance(member.get("attention"), list):
                for attention in member["attention"]:
                    collect(attention)
            if isinstance(member.get("episodes"), list):
                for episode in member["episodes"]:
                    if isinstance(episode, Mapping):
                        collect({"member_ref": member.get("member_ref"), "evidence_turn_ids": [episode.get("turn_id")]})
    if isinstance(proposal, Mapping):
        collect(proposal.get("contact"))
        if isinstance(proposal.get("attention"), list):
            for attention in proposal["attention"]:
                collect(attention)
    return pairs


class CompanionActivity:
    def __init__(self, db: Any, source: ContextSession, *, event_max: int, turn_max: int):
        self.db, self.source = db, source
        self.event_max, self.turn_max = event_max, turn_max
        self.reviews: dict[str, dict[str, object] | None] = {}

    async def review(self, review_ref: str) -> dict[str, object] | None:
        if review_ref in self.reviews:
            return self.reviews[review_ref]
        self.reviews[review_ref] = None
        row = await self.db.get(CompanionReview, review_ref)
        if (
            row is None
            or row.scope_id != self.source.scope_id
            or not await _persona_matches(self.db, self.source, row.persona_key)
        ):
            return None
        native = (await _native_turns(self.db, row.scope_id, [row])).get(review_ref)
        if native is None:
            # There is no saved source column on legacy review rows. Never infer one.
            return None
        turn, session = native
        if (
            session.reminder_task_ref
            or turn.id > self.turn_max
            or not await _persona_matches(self.db, session, row.persona_key)
        ):
            return None
        starts = list(
            (
                await self.db.scalars(
                    select(AgentEvent)
                    .where(
                        AgentEvent.turn_id == turn.id,
                        AgentEvent.event_type == "companion_reflection",
                        AgentEvent.status == "started",
                        AgentEvent.model_visible.is_(False),
                        AgentEvent.id <= self.event_max,
                    )
                    .limit(2)
                )
            ).all()
        )
        if len(starts) != 1:
            return None
        start = load_event_payload(starts[0])
        if start.get("lease_token") != review_ref:
            return None
        if "source_session_ref" in start:
            if start.get("source_session_ref") != self.source.session_ref or self.source.created_at > row.started_at:
                return None
        else:
            # Only native input/proposal evidence may relate an older review to a session.
            state = await self._private_payload(turn.id, "companion_state")
            proposal_event = await self._private_payload(turn.id, "companion_reflection", status="succeeded")
            proposal = proposal_event.get("proposal") if proposal_event else None
            pairs = _recorded_pairs(state, proposal)
            pairs.update(_recorded_pairs(None, {"contact": json_object(row.contact_proposal_json)}))
            ordered = sorted(pair for pair in pairs if pair[1] <= self.turn_max)
            found = False
            for offset in range(0, len(ordered), 100):
                links = await _evidence_links(
                    self.db, row, set(ordered[offset : offset + 100]), source_session_id=self.source.id
                )
                if links:
                    found = True
                    break
            if not found:
                return None
        result: dict[str, object] = {
            "review_ref": row.lease_token,
            "persona_key": row.persona_key,
            "status": row.status,
            "reason": row.reason[:1000],
            "started_at": utc(row.started_at),
            "finished_at": utc(row.finished_at),
            "committed": row.status == "completed" and row.committed_life_version is not None,
            "native_turn": _native_link(native),
        }
        self.reviews[review_ref] = result
        return result

    async def _private_payload(self, turn_id: int, kind: str, *, status: str | None = None) -> dict[str, object] | None:
        statement = select(AgentEvent).where(
            AgentEvent.turn_id == turn_id,
            AgentEvent.event_type == kind,
            AgentEvent.model_visible.is_(False),
            AgentEvent.id <= self.event_max,
        )
        if status is not None:
            statement = statement.where(AgentEvent.status == status)
        event = await self.db.scalar(statement.order_by(AgentEvent.sequence.desc()).limit(1))
        return load_event_payload(event) if event is not None else None

    async def contact(self, attempt_ref: str) -> tuple[AgentTurn, ContextSession] | None:
        contact = await self.db.get(CompanionContact, attempt_ref)
        if contact is None or contact.scope_id != self.source.scope_id or contact.turn_id is None:
            return None
        native = (await _contact_links(self.db, [contact])).get(attempt_ref)
        if native is None:
            return None
        # An ambiguous contact-to-turn binding cannot be safely deduplicated across pages.
        count = await self.db.scalar(
            select(func.count()).select_from(CompanionContact).where(CompanionContact.turn_id == contact.turn_id)
        )
        if count != 1:
            return None
        member = await self.db.get(CompanionMember, (contact.scope_id, contact.persona_key, contact.member_ref))
        if member is None or (member.user_id, member.platform_user_id) != (contact.user_id, contact.platform_user_id):
            return None
        turn = await self.db.get(AgentTurn, contact.turn_id)
        session = await self.db.get(ContextSession, turn.session_id)
        if (
            session.reminder_task_ref
            or turn.id > self.turn_max
            or not await _persona_matches(self.db, session, contact.persona_key)
        ):
            return None
        if not await _persona_matches(self.db, self.source, contact.persona_key):
            return None
        # Select just the provenance, not the context's system prompt or provider bodies.
        snapshot = await self.db.scalar(
            select(
                type_coerce(
                    func.json_extract(
                        case((func.json_valid(AgentEvent.payload_json) == 1, AgentEvent.payload_json), else_="{}"),
                        "$.selection.ordinary_chat",
                    ),
                    JSON,
                )
            )
            .where(
                AgentEvent.turn_id == turn.id,
                AgentEvent.event_type == "context_snapshot",
                AgentEvent.model_visible.is_(False),
                AgentEvent.id <= self.event_max,
            )
            .order_by(AgentEvent.sequence.desc())
            .limit(1)
        )
        if isinstance(snapshot, str):
            snapshot = json_object(snapshot)
        if (
            isinstance(snapshot, Mapping)
            and snapshot.get("session_ref") == self.source.session_ref
            and snapshot.get("status") in {"present", "empty"}
            and self.source.created_at <= turn.created_at
        ):
            return turn, session
        origin = await self.db.get(CompanionReview, contact.origin_review_token)
        if (
            origin is not None
            and origin.persona_key == contact.persona_key
            and await self.review(contact.origin_review_token) is not None
        ):
            return turn, session
        return None


async def review_activity_sources(db: Any, review_ref: str, *, event_max: int, turn_max: int) -> list[str]:
    """Identify archived reviews without inventing a current-session association."""
    review = await db.get(CompanionReview, review_ref)
    if review is None:
        return []
    native = (await _native_turns(db, review.scope_id, [review])).get(review_ref)
    if native is None or native[0].id > turn_max:
        return []
    turn, session = native
    reader = CompanionActivity(db, session, event_max=event_max, turn_max=turn_max)
    start = await reader._private_payload(turn.id, "companion_reflection", status="started")
    if start is None:
        return []
    sources: dict[int, ContextSession] = {}
    if "source_session_ref" in start:
        ref = start.get("source_session_ref")
        if not isinstance(ref, str) or not 1 <= len(ref) <= 200:
            return []
        source = await db.scalar(
            select(ContextSession)
            .where(
                ContextSession.session_ref == ref,
                ContextSession.scope_id == review.scope_id,
                ContextSession.purpose == "chat",
                ContextSession.reminder_task_ref == "",
            )
            .limit(1)
        )
        if source is not None:
            sources[source.id] = source
    else:
        state = await reader._private_payload(turn.id, "companion_state")
        proposal_event = await reader._private_payload(turn.id, "companion_reflection", status="succeeded")
        pairs = _recorded_pairs(state, proposal_event.get("proposal") if proposal_event else None)
        pairs.update(_recorded_pairs(None, {"contact": json_object(review.contact_proposal_json)}))
        ids = sorted({turn_id for _, turn_id in pairs})
        for offset in range(0, len(ids), 100):
            candidates = await db.scalars(
                select(ContextSession)
                .join(AgentTurn, AgentTurn.session_id == ContextSession.id)
                .where(
                    AgentTurn.id.in_(ids[offset : offset + 100]),
                    AgentTurn.id <= turn_max,
                    ContextSession.scope_id == review.scope_id,
                    ContextSession.purpose == "chat",
                    ContextSession.reminder_task_ref == "",
                )
                .distinct()
                .limit(100)
            )
            sources.update((source.id, source) for source in candidates)
    result = []
    for source in sources.values():
        projection = await CompanionActivity(db, source, event_max=event_max, turn_max=turn_max).review(review_ref)
        if projection is not None:
            result.append(source.session_ref)
    return sorted(result)
