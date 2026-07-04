from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nonebot_plugin_uninfo import Session  # noqa: TC002


@dataclass(slots=True)
class SwitchTarget:
    adapter: str
    group_id: str | None = None
    user_id: str | None = None


def adapter_key(session: Session) -> str:
    return str(session.adapter)


def current_target(session: Session) -> SwitchTarget:
    group_id = session.group.id if session.group else None
    user_id = session.user.id if session.user else None
    return SwitchTarget(
        adapter=adapter_key(session),
        group_id=group_id,
        user_id=user_id,
    )


def is_group_manager(session: Session) -> bool:
    role = session.member.role if session.member else None
    return bool(role and role.level >= 10)


def query_option(arp: Any, *paths: str) -> str | None:
    for path in paths:
        value = arp.query(path, None)
        if value is not None:
            return str(value)
    return None


def query_flag(arp: Any, *paths: str) -> bool:
    for path in paths:
        value = arp.query(path, None)
        if value is not None:
            return bool(value)
    return False
