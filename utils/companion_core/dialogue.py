"""Import-safe decisions and deadlines for a single companion conversation owner."""

from __future__ import annotations

from typing import Literal
from datetime import datetime, timedelta
from dataclasses import dataclass

DEFAULT_DIALOGUE_DELAY_SECONDS = 30
MAX_DIALOGUE_DELAY_SECONDS = 3600
AMBIENT_QUIET_SECONDS = 2
AMBIENT_CAP_SECONDS = 5

DialogueAction = Literal["observe", "wait", "end"]


@dataclass(frozen=True, slots=True)
class DialogueDecision:
    action: DialogueAction = "observe"
    seconds: int = DEFAULT_DIALOGUE_DELAY_SECONDS

    def __post_init__(self) -> None:
        if not isinstance(self.action, str) or self.action not in {"observe", "wait", "end"}:
            raise ValueError("Invalid companion dialogue action")
        if isinstance(self.seconds, bool) or not isinstance(self.seconds, int):
            raise ValueError("Companion dialogue seconds must be an integer")
        if not 1 <= self.seconds <= MAX_DIALOGUE_DELAY_SECONDS:
            raise ValueError(f"Companion dialogue seconds must be between 1 and {MAX_DIALOGUE_DELAY_SECONDS}")


def ambient_deadline(first: datetime, latest: datetime) -> datetime:
    """Debounce a burst, without postponing its oldest input indefinitely."""
    return min(first + timedelta(seconds=AMBIENT_CAP_SECONDS), latest + timedelta(seconds=AMBIENT_QUIET_SECONDS))


def decision_deadlines(decision: DialogueDecision, now: datetime) -> tuple[datetime | None, datetime | None]:
    """Observation has a gate, not an alarm; only an explicit wait owns an alarm."""
    if decision.action == "end":
        return None, None
    deadline = now + timedelta(seconds=decision.seconds)
    return deadline, deadline if decision.action == "wait" else None
