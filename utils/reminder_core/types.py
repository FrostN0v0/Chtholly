"""Immutable value objects for reminder scheduling policy.

The objects in this module deliberately contain no ORM, Entari, account, or
transport references.  They are safe to pass between a persistent store and
the pure policy functions.
"""

from __future__ import annotations

from enum import Enum
from datetime import datetime
from dataclasses import dataclass


class ScheduleSource(str, Enum):
    """Host-verified reason for accepting a schedule.

    This is intentionally not populated from model input.  A caller chooses
    :attr:`EXPLICIT_USER` by invoking ``parse_schedule`` for a verified user
    request, or :attr:`AUTONOMOUS_FOLLOWUP` by invoking
    ``schedule_autonomous`` for a host-authorized follow-up.
    """

    EXPLICIT_USER = "explicit_user"
    AUTONOMOUS_FOLLOWUP = "autonomous_followup"


@dataclass(frozen=True, slots=True)
class AcceptedSchedule:
    """A validated schedule represented in UTC and its IANA local zone."""

    utc: datetime
    local: datetime
    local_text: str
    timezone_name: str
    source: ScheduleSource


@dataclass(frozen=True, slots=True)
class WakeupHistoryEntry:
    """One previously accepted or delivered wakeup in UTC.

    The persistent store can construct this small value from an occurrence
    row.  The policy never keeps a process-local counter; each decision scans
    the supplied history for the relevant account/target/user tuple.
    """

    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class TaskState:
    """Bounded, plain task text safe to persist across wakeups."""

    goal: str
    background: str
    state: str


__all__ = [
    "AcceptedSchedule",
    "ScheduleSource",
    "TaskState",
    "WakeupHistoryEntry",
]
