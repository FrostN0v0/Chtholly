from __future__ import annotations

from nonebot.exception import IgnoredException
from nonebot.matcher import Matcher  # noqa: TC002
from nonebot.message import run_preprocessor
from nonebot_plugin_uninfo import Session, UniSession

from .config import resolve_state
from .registry import SELF_PLUGIN_NAMES, resolve_matcher_plugin
from .session import current_target


@run_preprocessor
async def plugin_switch_guard(
    matcher: Matcher,
    session: Session | None = UniSession(),
) -> None:
    plugin_name = resolve_matcher_plugin(matcher)
    if plugin_name is None or plugin_name in SELF_PLUGIN_NAMES:
        return
    if session is None:
        return
    target = current_target(session)
    state = resolve_state(
        plugin_name,
        target.adapter,
        group_id=target.group_id,
        user_id=target.user_id,
    )
    if state == "disabled":
        raise IgnoredException(f"plugin {plugin_name!r} is disabled")
