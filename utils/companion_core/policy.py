"""Pure bounded review scheduling and independent outbound-contact policy."""

from __future__ import annotations

import math
from datetime import datetime, timezone, timedelta
from dataclasses import fields, dataclass
from collections.abc import Mapping

UTC = timezone.utc
# Companion policy is for contemporary contacts. Shanghai has used UTC+08:00
# without daylight saving since 1991; a fixed offset avoids a tzdata dependency.
SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")
USER_QUIET_GRACE_MINUTES = 5

_LIMIT_RANGES = {
    "request_timeout": (5.0, 180.0),
    "reflection_interval_minutes": (10, 1440),
    "daily_reflections": (1, 48),
    "daily_contacts": (0, 12),
    "contact_interval_minutes": (30, 10080),
    "member_idle_days": (1, 90),
    "input_chars": (1000, 64000),
    "output_tokens": (256, 8192),
}


class CompanionPolicyError(ValueError):
    """Host policy inputs are not bounded or do not identify aware instants."""


def _limit_value(name: str, value: object) -> int | float:
    minimum, maximum = _LIMIT_RANGES[name]
    if name == "request_timeout":
        if type(value) not in (int, float):
            raise CompanionPolicyError(f"{name} must be a finite number, not a boolean or numeric string")
        try:
            normalized = float(value)
        except (OverflowError, ValueError) as exc:
            raise CompanionPolicyError(f"{name} must be a finite number") from exc
        if not math.isfinite(normalized):
            raise CompanionPolicyError(f"{name} must be finite")
    else:
        if type(value) is not int:
            raise CompanionPolicyError(f"{name} must be an integer, not a boolean or numeric string")
        normalized = value
    if not minimum <= normalized <= maximum:
        raise CompanionPolicyError(f"{name} must be between {minimum} and {maximum}")
    return normalized


@dataclass(frozen=True, slots=True)
class CompanionLimits:
    request_timeout: float = 60.0
    reflection_interval_minutes: int = 60
    daily_reflections: int = 12
    daily_contacts: int = 3
    contact_interval_minutes: int = 180
    member_idle_days: int = 14
    input_chars: int = 32000
    output_tokens: int = 4096

    def __post_init__(self) -> None:
        for field in fields(self):
            value = _limit_value(field.name, getattr(self, field.name))
            object.__setattr__(self, field.name, value)


def normalize_limits(config: Mapping[str, object]) -> CompanionLimits:
    """Accept only core limit names; the host strips its companion_ prefix."""
    if not isinstance(config, Mapping):
        raise CompanionPolicyError("companion limits must be a mapping")
    if any(not isinstance(name, str) or name not in _LIMIT_RANGES for name in config):
        raise CompanionPolicyError("companion limits contains unknown names")
    return CompanionLimits(**dict(config))


def _aware_utc(value: datetime, *, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CompanionPolicyError(f"{label} must be timezone-aware")
    try:
        if value.utcoffset() is None:
            raise CompanionPolicyError(f"{label} must be timezone-aware")
        return value.astimezone(UTC)
    except (OverflowError, ValueError) as exc:
        raise CompanionPolicyError(f"{label} cannot be represented as an aware UTC instant") from exc


def _nonnegative_count(value: int, *, label: str) -> int:
    if type(value) is not int or value < 0:
        raise CompanionPolicyError(f"{label} must be a nonnegative integer")
    return value


def next_review_at(now: datetime, minutes: int, *, limits: CompanionLimits) -> datetime:
    """Clamp model scheduling suggestions independently of contact eligibility.

    The reflection interval is the host's preferred default, not a minimum
    delay: a grounded concern may warrant an earlier internal review. Daily
    reservations are enforced by the durable store, never by this timestamp.
    """
    current = _aware_utc(now, label="now")
    if not isinstance(limits, CompanionLimits):
        raise CompanionPolicyError("limits must be CompanionLimits")
    if type(minutes) is not int:
        raise CompanionPolicyError("review minutes must be an integer")
    bounded = min(1440, max(10, minutes))
    try:
        return current + timedelta(minutes=bounded)
    except OverflowError as exc:
        raise CompanionPolicyError("review time exceeds the supported datetime range") from exc


def contact_ready(
    now: datetime,
    *,
    last_contact: datetime | None,
    last_user_at: datetime,
    contacts_today: int,
    limits: CompanionLimits,
    unanswered_contacts: int = 0,
) -> bool:
    """Evaluate host facts, not message wording or the presence of a task.

    contacts_today includes active and unknown durable reservations for the
    Shanghai calendar day. last_contact includes started unknown attempts;
    unanswered_contacts counts started attempts since the last real user
    chat. The host separately fences mode, member, authorization and active
    turns before every send. A false result never forbids internal reflection.
    """
    current = _aware_utc(now, label="now")
    user_time = _aware_utc(last_user_at, label="last_user_at")
    contact_time = _aware_utc(last_contact, label="last_contact") if last_contact is not None else None
    count = _nonnegative_count(contacts_today, label="contacts_today")
    unanswered = _nonnegative_count(unanswered_contacts, label="unanswered_contacts")
    if not isinstance(limits, CompanionLimits):
        raise CompanionPolicyError("limits must be CompanionLimits")
    try:
        local = current.astimezone(SHANGHAI)
    except (OverflowError, ValueError) as exc:
        raise CompanionPolicyError("now cannot be represented in Shanghai") from exc
    if local.hour >= 23 or local.hour < 7:
        return False
    if count >= limits.daily_contacts:
        return False
    user_age = current - user_time
    if user_age < timedelta(minutes=USER_QUIET_GRACE_MINUTES) or user_age > timedelta(days=limits.member_idle_days):
        return False
    if contact_time is None:
        return unanswered == 0
    # No reply reduces outreach density without inventing resentment or
    # treating silence as a failed relationship. A real chat resets the count.
    cooldown = min(limits.contact_interval_minutes * 2 ** min(unanswered, 5), 7 * 24 * 60)
    return current - contact_time >= timedelta(minutes=cooldown)
