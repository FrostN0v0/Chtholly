from __future__ import annotations

from datetime import datetime, timezone

import pytest

from utils.reminder_core import (
    ScheduleSource,
    ScheduleInPastError,
    InvalidScheduleError,
    InvalidTaskStateError,
    parse_schedule,
    validate_task_text,
    schedule_autonomous,
)

UTC = timezone.utc


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_parse_schedule_returns_matching_utc_and_local_values() -> None:
    result = parse_schedule(
        "2026-01-02T09:30:00+08:00",
        "Asia/Shanghai",
        now=_utc("2026-01-01T00:00:00Z"),
    )

    assert result.utc == _utc("2026-01-02T01:30:00Z")
    assert result.local.isoformat() == "2026-01-02T09:30:00+08:00"
    assert result.local_text == result.local.isoformat()
    assert result.source is ScheduleSource.EXPLICIT_USER


def test_parse_schedule_requires_explicit_matching_offset() -> None:
    now = _utc("2026-01-01T00:00:00Z")

    with pytest.raises(InvalidScheduleError):
        parse_schedule("2026-01-02T09:30:00", "Asia/Shanghai", now=now)
    with pytest.raises(InvalidScheduleError):
        parse_schedule("2026-01-02T09:30:00+09:00", "Asia/Shanghai", now=now)
    with pytest.raises(ScheduleInPastError):
        parse_schedule("2025-12-31T23:59:59+08:00", "Asia/Shanghai", now=now)
    with pytest.raises(InvalidScheduleError):
        parse_schedule("2026-02-30T09:30:00+08:00", "Asia/Shanghai", now=now)


def test_parse_schedule_rejects_dst_gap_and_wrong_overlap_offset() -> None:
    now = _utc("2024-11-01T00:00:00Z")

    # 02:30 does not exist when New York jumps from 01:59:59 to 03:00.
    with pytest.raises(InvalidScheduleError):
        parse_schedule("2024-03-10T02:30:00-05:00", "America/New_York", now=now)

    overlap_now = _utc("2024-11-03T00:00:00Z")
    with pytest.raises(InvalidScheduleError):
        parse_schedule("2024-11-03T01:30:00-06:00", "America/New_York", now=overlap_now)


def test_parse_schedule_accepts_each_explicit_dst_overlap_fold() -> None:
    now = _utc("2024-11-03T00:00:00Z")

    first = parse_schedule("2024-11-03T01:30:00-04:00", "America/New_York", now=now)
    second = parse_schedule("2024-11-03T01:30:00-05:00", "America/New_York", now=now)

    assert first.utc == _utc("2024-11-03T05:30:00Z")
    assert second.utc == _utc("2024-11-03T06:30:00Z")
    assert first.utc < second.utc


def test_autonomous_quiet_window_has_open_and_closed_boundaries() -> None:
    now = _utc("2026-01-01T00:00:00Z")

    before_start = schedule_autonomous(_utc("2026-01-01T22:59:00Z"), "UTC", previous_wakeups=(), now=now)
    at_start = schedule_autonomous(_utc("2026-01-01T23:00:00Z"), "UTC", previous_wakeups=(), now=now)
    at_end = schedule_autonomous(_utc("2026-01-01T07:00:00Z"), "UTC", previous_wakeups=(), now=now)
    before_end = schedule_autonomous(_utc("2026-01-01T06:59:00Z"), "UTC", previous_wakeups=(), now=now)

    assert before_start.utc == _utc("2026-01-01T22:59:00Z")
    assert at_start.utc == _utc("2026-01-02T07:00:00Z")
    assert at_end.utc == _utc("2026-01-01T07:00:00Z")
    assert before_end.utc == _utc("2026-01-01T07:00:00Z")


def test_autonomous_minimum_interval_uses_persistent_history() -> None:
    now = _utc("2026-01-01T00:00:00Z")
    result = schedule_autonomous(
        _utc("2026-01-01T10:00:00Z"),
        "UTC",
        previous_wakeups=(_utc("2026-01-01T09:45:00Z"),),
        now=now,
    )

    assert result.utc == _utc("2026-01-01T10:15:00Z")


def test_autonomous_daily_limit_uses_local_calendar_day_and_counts_silent_wakeups() -> None:
    now = _utc("2026-01-01T00:00:00Z")
    # These UTC times are all 2026-01-01 in Shanghai too; the third is a
    # silent wakeup and is still part of the supplied persistent history.
    history = (
        _utc("2026-01-01T00:00:00Z"),
        _utc("2026-01-01T01:00:00Z"),
        _utc("2026-01-01T02:00:00Z"),
    )
    result = schedule_autonomous(
        _utc("2026-01-01T04:00:00Z"),
        "Asia/Shanghai",
        previous_wakeups=history,
        now=now,
    )

    assert result.local.isoformat() == "2026-01-02T07:00:00+08:00"
    assert result.utc == _utc("2026-01-01T23:00:00Z")
    assert result.source is ScheduleSource.AUTONOMOUS_FOLLOWUP


def test_autonomous_crosses_day_after_quiet_and_still_honors_minimum_interval() -> None:
    now = _utc("2026-01-01T00:00:00Z")
    result = schedule_autonomous(
        _utc("2026-01-01T22:59:00Z"),
        "UTC",
        previous_wakeups=(_utc("2026-01-01T22:45:00Z"),),
        now=now,
    )

    assert result.utc == _utc("2026-01-02T07:00:00Z")


def test_explicit_user_schedule_is_not_silently_postponed_by_quiet_hours() -> None:
    result = parse_schedule(
        "2026-01-01T23:30:00+00:00",
        "UTC",
        now=_utc("2026-01-01T00:00:00Z"),
    )

    assert result.utc == _utc("2026-01-01T23:30:00Z")
    assert result.source is ScheduleSource.EXPLICIT_USER


def test_validate_task_text_enforces_bounds_and_rejects_privileged_refs() -> None:
    result = validate_task_text("g" * 1000, "b" * 3000, "s" * 2000)
    assert result.goal == "g" * 1000
    assert result.background == "b" * 3000
    assert result.state == "s" * 2000

    with pytest.raises(InvalidTaskStateError):
        validate_task_text("", "", "")
    with pytest.raises(InvalidTaskStateError):
        validate_task_text("g" * 1001)
    with pytest.raises(InvalidTaskStateError):
        validate_task_text("goal", "background", '{"media_ref":"temporary"}')
    with pytest.raises(InvalidTaskStateError):
        validate_task_text("goal", "background", "state" * 401)
