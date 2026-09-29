"""Generation-local proposal state for scheduled reminder decisions."""

from __future__ import annotations

from datetime import datetime
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from collections.abc import Iterator


@dataclass(frozen=True, slots=True)
class ReminderDecision:
    """A host-validated, generation-local proposal for the claimed task."""

    task_ref: str
    status: str
    next_due_at: datetime | None = None
    timezone_name: str = ""
    continuation_state: str = ""
    reason: str = ""


@dataclass(slots=True)
class _DecisionState:
    decision: ReminderDecision | None = None
    active: bool = True


_ACTIVE_REMINDER_DECISION: ContextVar[_DecisionState | None] = ContextVar("llm_chat_reminder_decision", default=None)


@contextmanager
def reminder_decision_scope() -> Iterator[None]:
    """Clear a scheduled generation's proposal and restore its parent state."""

    # Native tools run in child tasks; ContextVar assignments do not flow back.
    state = _DecisionState()
    token = _ACTIVE_REMINDER_DECISION.set(state)
    try:
        yield
    finally:
        state.active = False
        state.decision = None
        _ACTIVE_REMINDER_DECISION.reset(token)


def current_reminder_decision() -> ReminderDecision | None:
    """Return the current generation's validated proposal, if any."""

    state = _ACTIVE_REMINDER_DECISION.get()
    return state.decision if state is not None and state.active else None


def propose_reminder_decision(decision: ReminderDecision) -> ReminderDecision:
    """Store exactly one proposal in the current generation."""

    if not isinstance(decision, ReminderDecision):
        raise TypeError("decision must be a ReminderDecision")
    state = _ACTIVE_REMINDER_DECISION.get()
    if state is None or not state.active:
        raise ValueError("a reminder decision requires an active scheduled generation")
    if state.decision is not None:
        raise ValueError("a reminder decision has already been proposed")
    state.decision = decision
    return decision


__all__ = [
    "ReminderDecision",
    "current_reminder_decision",
    "propose_reminder_decision",
    "reminder_decision_scope",
]
