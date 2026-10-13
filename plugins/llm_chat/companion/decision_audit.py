"""Private host decisions appended only after a native reflection has settled."""

from __future__ import annotations

from typing import Any, Literal
from datetime import datetime
from dataclasses import field, dataclass
from collections.abc import Mapping, Callable

from sqlalchemy import func, select

from .types import ReviewClaim
from ..models import AgentTurn, AgentEvent, ContextSession
from .persistence import dumps, naive

DecisionStatus = Literal["not_proposed", "blocked", "reserved", "interrupted", "failed"]


@dataclass(slots=True)
class ContactDecision:
    status: DecisionStatus = "failed"
    reason: str = "reflection_failed"
    facts: dict[str, object] = field(default_factory=dict)
    attempt_ref: str | None = None
    checked_at: datetime | None = None

    def set(
        self,
        status: DecisionStatus,
        reason: str,
        *,
        facts: Mapping[str, object] | None = None,
        at: datetime | None = None,
    ) -> None:
        self.status, self.reason = status, reason
        self.facts = dict(facts or {})
        if at is not None:
            self.checked_at = at


async def persist_review_decision(
    claim: ReviewClaim,
    decision: ContactDecision,
    *,
    now: datetime,
    session_factory: Callable[[], Any],
) -> bool:
    """The worker is the sole writer after the reflection recorder's final flush."""
    async with session_factory() as db, db.begin():
        native = await db.scalar(
            select(AgentTurn.id)
            .join(ContextSession, ContextSession.id == AgentTurn.session_id)
            .join(AgentEvent, AgentEvent.turn_id == AgentTurn.id)
            .where(
                ContextSession.scope_id == claim.scope_id,
                ContextSession.purpose == "companion_reflection",
                AgentTurn.trigger_kind == "reflection",
                AgentTurn.finished_at.is_not(None),
                AgentEvent.event_type == "companion_reflection",
                AgentEvent.status == "started",
                func.json_extract(AgentEvent.payload_json, "$.lease_token") == claim.lease_token,
            )
        )
        if native is None:
            return False
        previous = await db.scalar(
            select(AgentEvent.id).where(
                AgentEvent.turn_id == native,
                AgentEvent.event_type == "companion_contact_decision",
            )
        )
        if previous is not None:
            return True
        sequence = await db.scalar(select(func.max(AgentEvent.sequence)).where(AgentEvent.turn_id == native))
        payload: dict[str, object] = {
            "status": decision.status,
            "reason": decision.reason,
            "checked_at": (decision.checked_at or now).isoformat(),
            "review_ref": claim.lease_token,
            "facts": decision.facts,
        }
        if decision.attempt_ref is not None:
            payload["attempt_ref"] = decision.attempt_ref
        db.add(
            AgentEvent(
                turn_id=native,
                sequence=(sequence or 0) + 1,
                event_type="companion_contact_decision",
                status=decision.status,
                effect="none",
                model_visible=False,
                payload_json=dumps(payload),
                created_at=naive(now),
            )
        )
        return True
