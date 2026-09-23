"""Pure reminder time, contact-window, and bounded-state policy.

This module intentionally has no ORM or Entari imports.  A host supplies the
verified source of a schedule by choosing either :func:`parse_schedule` for a
user request or :func:`schedule_autonomous` for an already-authorized model
follow-up.  Model payloads never choose that source themselves.
"""

from __future__ import annotations

import re
from typing import Final
from datetime import time, datetime, timezone, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from collections.abc import Iterable

from .types import TaskState, ScheduleSource, AcceptedSchedule, WakeupHistoryEntry

UTC = timezone.utc

MAX_GOAL_LENGTH: Final = 1000
MAX_BACKGROUND_LENGTH: Final = 3000
MAX_STATE_LENGTH: Final = 2000


class ReminderPolicyError(ValueError):
    """Base error for invalid reminder policy input."""


class InvalidScheduleError(ReminderPolicyError):
    """The timestamp, timezone, or scheduling policy input is invalid."""


class ScheduleInPastError(InvalidScheduleError):
    """A schedule is earlier than the host-supplied current time."""


class InvalidTaskStateError(ReminderPolicyError):
    """Task text is not bounded plain state."""


# These are capability-bearing values which must never be smuggled into a
# durable task summary.  Match key-like spellings, not ordinary prose words.
_PRIVILEGED_REF_RE = re.compile(
    r"(?i)(?<![a-z0-9])"
    r"(?:media_ref|image_ref|participant_ref|session_ref|channel_ref|account_ref|"
    r"execution_ref|message_ref|lease_token|access_token|refresh_token|cookie_ref)"
    r"(?![a-z0-9])"
)


def _timezone(name: str) -> ZoneInfo:
    if not isinstance(name, str) or not name or name.strip() != name:
        raise InvalidScheduleError("timezone_name must be a non-empty IANA timezone name")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise InvalidScheduleError(f"unknown IANA timezone: {name!r}") from exc


def _utc(value: datetime, *, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise InvalidScheduleError(f"{label} must be timezone-aware")
    try:
        offset = value.utcoffset()
    except (OverflowError, ValueError) as exc:
        raise InvalidScheduleError(f"{label} has an invalid UTC offset") from exc
    if offset is None:
        raise InvalidScheduleError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _parse_explicit(value: str) -> datetime:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise InvalidScheduleError("schedule must be an ISO-8601 timestamp with an explicit offset")
    candidate_text = "+00:00" if value.endswith(("Z", "z")) else None
    if candidate_text is not None:
        value = value[:-1] + candidate_text
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise InvalidScheduleError("schedule must be a valid ISO-8601 timestamp") from exc
    try:
        offset = parsed.utcoffset()
    except (OverflowError, ValueError) as exc:
        raise InvalidScheduleError("schedule has an invalid UTC offset") from exc
    if parsed.tzinfo is None or offset is None:
        raise InvalidScheduleError("schedule must include an explicit UTC offset")
    return parsed


def _accepted(
    candidate_utc: datetime,
    *,
    zone: ZoneInfo,
    timezone_name: str,
    source: ScheduleSource,
) -> AcceptedSchedule:
    local = candidate_utc.astimezone(zone)
    return AcceptedSchedule(
        utc=candidate_utc,
        local=local,
        local_text=local.isoformat(),
        timezone_name=timezone_name,
        source=source,
    )


def parse_schedule(value: str, timezone_name: str, *, now: datetime) -> AcceptedSchedule:
    """Validate a user-specified offset timestamp against an IANA timezone.

    The supplied offset must describe the zone at the exact instant.  This
    rejects both nonexistent local times in a DST gap and an ambiguous local
    hour whose offset was omitted or does not select either valid fold.  The
    returned values are immutable, aware datetimes.
    """

    zone = _timezone(timezone_name)
    now_utc = _utc(now, label="now")
    parsed = _parse_explicit(value)
    candidate_utc = parsed.astimezone(UTC)
    local = candidate_utc.astimezone(zone)
    supplied_local = parsed.replace(tzinfo=None)
    supplied_offset = parsed.utcoffset()
    if local.replace(tzinfo=None) != supplied_local or local.utcoffset() != supplied_offset:
        raise InvalidScheduleError(
            "schedule offset and local clock do not match the supplied IANA timezone (including DST gap/overlap rules)"
        )
    if candidate_utc < now_utc:
        raise ScheduleInPastError("schedule is in the past")
    return _accepted(candidate_utc, zone=zone, timezone_name=timezone_name, source=ScheduleSource.EXPLICIT_USER)


def _history_utc(previous_wakeups: Iterable[datetime | WakeupHistoryEntry]) -> tuple[datetime, ...]:
    try:
        entries = tuple(previous_wakeups)
    except TypeError as exc:
        raise InvalidScheduleError("previous_wakeups must be an iterable of aware datetimes") from exc
    result: list[datetime] = []
    for entry in entries:
        value = entry.occurred_at if isinstance(entry, WakeupHistoryEntry) else entry
        result.append(_utc(value, label="previous_wakeup"))
    return tuple(result)


def _clock_minutes(local: datetime) -> float:
    return local.hour * 60 + local.minute + local.second / 60 + local.microsecond / 60_000_000


def _in_quiet_window(local: datetime, quiet_start: int, quiet_end: int) -> bool:
    clock = _clock_minutes(local)
    if quiet_start < quiet_end:
        return quiet_start * 60 <= clock < quiet_end * 60
    if quiet_start > quiet_end:
        return clock >= quiet_start * 60 or clock < quiet_end * 60
    return False


def _wall_candidates(naive: datetime, zone: ZoneInfo) -> tuple[datetime, ...]:
    """Return valid UTC instants for a wall time, in chronological order."""

    values: dict[datetime, None] = {}
    for fold in (0, 1):
        aware = naive.replace(tzinfo=zone, fold=fold)
        instant = aware.astimezone(UTC)
        round_trip = instant.astimezone(zone)
        if round_trip.replace(tzinfo=None) == naive and round_trip.utcoffset() == aware.utcoffset():
            values[instant] = None
    return tuple(sorted(values))


def _wall_to_utc(naive: datetime, zone: ZoneInfo) -> datetime:
    candidates = _wall_candidates(naive, zone)
    if candidates:
        # A host-created quiet-hour boundary does not have a supplied offset;
        # choosing the earlier fold is deterministic and avoids skipping a
        # valid occurrence.  Explicit model/user timestamps use parse_schedule
        # and must select their own offset.
        return candidates[0]
    # A configurable boundary could itself fall in a DST gap.  Move forward
    # to the first representable wall minute rather than inventing an offset.
    probe = naive
    for _ in range(3 * 60 + 1):
        probe += timedelta(minutes=1)
        candidates = _wall_candidates(probe, zone)
        if candidates:
            return candidates[0]
    raise InvalidScheduleError("cannot resolve a local scheduling boundary in the supplied timezone")


def _quiet_target(local: datetime, quiet_start: int, quiet_end: int) -> datetime:
    target_date = local.date()
    if quiet_start > quiet_end and _clock_minutes(local) >= quiet_start * 60:
        target_date += timedelta(days=1)
    return datetime.combine(target_date, time(hour=quiet_end))


def _next_day_target(local: datetime, quiet_start: int, quiet_end: int) -> datetime:
    target_date = local.date() + timedelta(days=1)
    # 07:00 is the ordinary autonomous start.  For custom windows, the end
    # boundary is the first allowed slot after the overnight quiet interval.
    target_hour = quiet_end if quiet_start != quiet_end else 0
    return datetime.combine(target_date, time(hour=target_hour))


def schedule_autonomous(
    candidate_utc: datetime,
    timezone_name: str,
    *,
    previous_wakeups: Iterable[datetime | WakeupHistoryEntry],
    now: datetime,
    min_interval_minutes: int = 30,
    daily_limit: int = 3,
    quiet_start: int = 23,
    quiet_end: int = 7,
) -> AcceptedSchedule:
    """Apply host autonomous-contact limits to a candidate UTC instant.

    ``previous_wakeups`` is persistent history already scoped to one
    (account, target, user) tuple.  It is scanned on every call; no process
    local counters are created.  A candidate is postponed, never discarded,
    when it falls in the quiet window, violates the minimum interval, or
    would exceed the local-calendar-day limit.  This function is only for
    verified autonomous follow-ups; explicit user schedules use
    :func:`parse_schedule` and therefore are not silently postponed by this
    policy.
    """

    zone = _timezone(timezone_name)
    now_utc = _utc(now, label="now")
    candidate = _utc(candidate_utc, label="candidate_utc")
    if candidate < now_utc:
        raise ScheduleInPastError("candidate schedule is in the past")
    if isinstance(min_interval_minutes, bool) or not isinstance(min_interval_minutes, int) or min_interval_minutes < 0:
        raise InvalidScheduleError("min_interval_minutes must be a non-negative integer")
    if isinstance(daily_limit, bool) or not isinstance(daily_limit, int) or daily_limit < 1:
        raise InvalidScheduleError("daily_limit must be a positive integer")
    for name, hour in (("quiet_start", quiet_start), ("quiet_end", quiet_end)):
        if isinstance(hour, bool) or not isinstance(hour, int) or not 0 <= hour <= 23:
            raise InvalidScheduleError(f"{name} must be an hour from 0 through 23")

    history = _history_utc(previous_wakeups)
    latest = max(history, default=None)
    minimum_delta = timedelta(minutes=min_interval_minutes)

    while True:
        before = candidate
        local = candidate.astimezone(zone)

        if _in_quiet_window(local, quiet_start, quiet_end):
            candidate = _wall_to_utc(_quiet_target(local, quiet_start, quiet_end), zone)

        if latest is not None and candidate < latest + minimum_delta:
            candidate = latest + minimum_delta

        local = candidate.astimezone(zone)
        local_date = local.date()
        same_day = sum(1 for item in history if item.astimezone(zone).date() == local_date)
        if same_day >= daily_limit:
            candidate = _wall_to_utc(_next_day_target(local, quiet_start, quiet_end), zone)

        if candidate == before:
            break

    if candidate < now_utc:
        raise ScheduleInPastError("effective schedule is in the past")
    return _accepted(candidate, zone=zone, timezone_name=timezone_name, source=ScheduleSource.AUTONOMOUS_FOLLOWUP)


def _bounded_text(value: object, *, label: str, maximum: int, required: bool) -> str:
    if not isinstance(value, str):
        raise InvalidTaskStateError(f"{label} must be plain text")
    if len(value) > maximum:
        raise InvalidTaskStateError(f"{label} exceeds its {maximum}-character limit")
    if required and not value.strip():
        raise InvalidTaskStateError(f"{label} must not be empty")
    if "\x00" in value or any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise InvalidTaskStateError(f"{label} contains invalid control text")
    if _PRIVILEGED_REF_RE.search(value):
        raise InvalidTaskStateError(f"{label} contains a privileged short-lived reference")
    return value


def validate_task_text(goal: object, background: object = "", state: object = "") -> TaskState:
    """Validate the bounded plain text persisted with a reminder task."""

    return TaskState(
        goal=_bounded_text(goal, label="goal", maximum=MAX_GOAL_LENGTH, required=True),
        background=_bounded_text(background, label="background", maximum=MAX_BACKGROUND_LENGTH, required=False),
        state=_bounded_text(state, label="state", maximum=MAX_STATE_LENGTH, required=False),
    )


__all__ = [
    "MAX_BACKGROUND_LENGTH",
    "MAX_GOAL_LENGTH",
    "MAX_STATE_LENGTH",
    "InvalidScheduleError",
    "InvalidTaskStateError",
    "ReminderPolicyError",
    "ScheduleInPastError",
    "parse_schedule",
    "schedule_autonomous",
    "validate_task_text",
]
