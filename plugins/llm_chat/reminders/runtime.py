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


_ACTIVE_REMINDER_DECISION: ContextVar[ReminderDecision | None] = ContextVar(
    "llm_chat_reminder_decision",
    default=None,
)


@contextmanager
def reminder_decision_scope() -> Iterator[None]:
    """Clear a scheduled generation's proposal and restore its parent state."""

    token = _ACTIVE_REMINDER_DECISION.set(None)
    try:
        yield
    finally:
        _ACTIVE_REMINDER_DECISION.reset(token)


def current_reminder_decision() -> ReminderDecision | None:
    """Return the current generation's validated proposal, if any."""

    return _ACTIVE_REMINDER_DECISION.get()


def propose_reminder_decision(decision: ReminderDecision) -> ReminderDecision:
    """Store exactly one proposal in the current generation."""

    if not isinstance(decision, ReminderDecision):
        raise TypeError("decision must be a ReminderDecision")
    if _ACTIVE_REMINDER_DECISION.get() is not None:
        raise ValueError("a reminder decision has already been proposed")
    _ACTIVE_REMINDER_DECISION.set(decision)
    return decision


__all__ = [
    "ReminderDecision",
    "current_reminder_decision",
    "propose_reminder_decision",
    "reminder_decision_scope",
]
