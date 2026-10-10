"""Immutable capabilities and detached snapshots returned by companion persistence."""

from __future__ import annotations

from typing import Any
from datetime import datetime
from dataclasses import field, dataclass
from collections.abc import Mapping

from utils.companion_core import LifeState


class CompanionError(RuntimeError):
    """A companion persistence or authorization failure."""


class CompanionConflict(CompanionError):
    """A revision or lease no longer authorizes the requested operation."""


class CompanionCorruptState(CompanionError):
    """Persisted generated state failed strict validation."""


class FrozenDict(dict):
    """A JSON-serializable dictionary that cannot be modified after construction."""

    def _immutable(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("Snapshot dictionaries are immutable")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _immutable

    def __copy__(self) -> FrozenDict:
        return self

    def __deepcopy__(self, memo: dict) -> FrozenDict:
        return self


def freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return FrozenDict({key: freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class ModeSnapshot:
    scope_id: int
    mode: str
    revision: int
    persona_key: str
    next_review_at: datetime | None
    context_revision: int


@dataclass(frozen=True, slots=True)
class MemberSnapshot:
    scope_id: int
    persona_key: str
    member_ref: str
    user_id: str
    platform_user_id: str
    display_name: str
    version: int
    last_turn_id: int
    last_user_at: datetime
    last_contact_at: datetime | None


@dataclass(frozen=True, slots=True)
class ReviewClaim:
    scope_id: int
    persona_key: str
    mode_revision: int
    context_revision: int
    lease_token: str
    lease_deadline: datetime
    life_version: int


@dataclass(frozen=True, slots=True)
class ReflectionSnapshot:
    mode: ModeSnapshot
    life: LifeState
    life_version: int
    members: tuple[MemberSnapshot, ...]
    attention: tuple[dict, ...]
    recent_contacts: tuple[dict, ...]
    member_turn_ids: Mapping[str, tuple[int, ...]] = field(default_factory=FrozenDict)


@dataclass(frozen=True, slots=True)
class ContactClaim:
    attempt_ref: str
    scope_id: int
    persona_key: str
    mode_revision: int
    context_revision: int
    member_ref: str
    member_version: int
    user_id: str
    platform_user_id: str
    display_name: str
    lease_token: str
    lease_deadline: datetime
