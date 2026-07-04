from __future__ import annotations

from arclet.alconna import Alconna, Args, Arparma, Option, Subcommand
from nonebot.adapters import Bot, Event  # noqa: TC002
from nonebot.permission import SUPERUSER
from nonebot_plugin_alconna import on_alconna
from nonebot_plugin_uninfo import Session, UniSession

from .config import (
    load_config,
    resolve_state,
    set_global_state,
    set_group_state,
    set_user_state,
)
from .models import PluginState  # noqa: TC001
from .registry import (
    SELF_PLUGIN_NAMES,
    get_canonical_plugin_names,
    resolve_user_plugin_name,
)
from .session import current_target, is_group_manager, query_flag, query_option

plugin_cmd = on_alconna(
    Alconna(
        "/plugin",
        Subcommand("list"),
        Subcommand("reload"),
        Subcommand("status", Args["plugin", str]),
        Subcommand(
            "enable",
            Args["plugin", str],
            Option("--global"),
            Option("--group", Args["group_id", str]),
            Option("--user", Args["user_id", str]),
            Option("--adapter", Args["adapter", str]),
        ),
        Subcommand(
            "disable",
            Args["plugin", str],
            Option("--global"),
            Option("--group", Args["group_id", str]),
            Option("--user", Args["user_id", str]),
            Option("--adapter", Args["adapter", str]),
        ),
    ),
    priority=5,
    block=True,
)


async def _is_superuser(bot: Bot, event: Event) -> bool:
    return await SUPERUSER(bot, event)


async def _can_manage_current(bot: Bot, event: Event, session: Session | None) -> bool:
    if await _is_superuser(bot, event):
        return True
    return bool(session and session.group and is_group_manager(session))


async def _require_superuser(bot: Bot, event: Event) -> None:
    if not await _is_superuser(bot, event):
        await plugin_cmd.finish("Permission denied.")


async def _require_current_manager(
    bot: Bot,
    event: Event,
    session: Session | None,
) -> None:
    if not await _can_manage_current(bot, event, session):
        await plugin_cmd.finish("Permission denied.")


def _resolve_plugin_or_error(value: str) -> str:
    plugin_name = resolve_user_plugin_name(value)
    if plugin_name is None:
        raise ValueError(f"unknown plugin: {value}")
    if plugin_name in SELF_PLUGIN_NAMES:
        raise ValueError("plugin_manager cannot manage itself")
    return plugin_name


def _plugin_arg(arp: Arparma, action: str) -> str:
    value = arp.query(f"{action}.plugin", None)
    if value is None:
        raise ValueError("plugin argument is required")
    return str(value)


def _option_values(
    arp: Arparma,
    action: str,
) -> tuple[bool, str | None, str | None, str | None]:
    global_ = query_flag(arp, f"{action}.global.value")
    group_id = query_option(arp, f"{action}.group.group_id")
    user_id = query_option(arp, f"{action}.user.user_id")
    adapter = query_option(arp, f"{action}.adapter.adapter")
    return global_, group_id, user_id, adapter


async def _ensure_scope_permission(
    bot: Bot,
    event: Event,
    session: Session | None,
    *,
    global_: bool,
    group_id: str | None,
    user_id: str | None,
    adapter: str | None,
) -> None:
    if global_ or group_id is not None or user_id is not None or adapter is not None:
        await _require_superuser(bot, event)
        return
    await _require_current_manager(bot, event, session)


def _current_status(plugin_name: str, session: Session | None) -> str:
    if session is None:
        return f"{plugin_name}: enabled"
    target = current_target(session)
    state = resolve_state(
        plugin_name,
        target.adapter,
        group_id=target.group_id,
        user_id=target.user_id,
    )
    scope = f"group {target.group_id}" if target.group_id else f"user {target.user_id}"
    return f"{plugin_name}: {state} in {target.adapter} {scope}"


def _set_state(
    plugin_value: str,
    state: PluginState,
    session: Session | None,
    *,
    global_: bool,
    group_id: str | None,
    user_id: str | None,
    adapter: str | None,
) -> str:
    plugin_name = _resolve_plugin_or_error(plugin_value)
    target = current_target(session) if session else None
    adapter_key = adapter or (target.adapter if target else None)

    if global_:
        set_global_state(plugin_name, state)
        return f"{plugin_name}: global -> {state}"
    if group_id is not None:
        if adapter_key is None:
            raise ValueError("--adapter is required outside a known session")
        set_group_state(plugin_name, adapter_key, group_id, state)
        return f"{plugin_name}: group {group_id} -> {state}"
    if user_id is not None:
        if adapter_key is None:
            raise ValueError("--adapter is required outside a known session")
        set_user_state(plugin_name, adapter_key, user_id, state)
        return f"{plugin_name}: user {user_id} -> {state}"
    if target and target.group_id:
        set_group_state(plugin_name, target.adapter, target.group_id, state)
        return f"{plugin_name}: group {target.group_id} -> {state}"
    if target and target.user_id:
        set_user_state(plugin_name, target.adapter, target.user_id, state)
        return f"{plugin_name}: user {target.user_id} -> {state}"
    raise ValueError("no target session found")


@plugin_cmd.handle()
async def handle_plugin_command(
    arp: Arparma,
    bot: Bot,
    event: Event,
    session: Session | None = UniSession(),
) -> None:
    try:
        if arp.query("list", None) is not None:
            await _require_current_manager(bot, event, session)
            names = sorted(get_canonical_plugin_names())
            await plugin_cmd.finish("\n".join(names) if names else "No plugins loaded.")

        if arp.query("reload", None) is not None:
            await _require_superuser(bot, event)
            load_config()
            await plugin_cmd.finish("Plugin switch config reloaded.")

        if arp.query("status", None) is not None:
            await _require_current_manager(bot, event, session)
            plugin_name = _resolve_plugin_or_error(_plugin_arg(arp, "status"))
            await plugin_cmd.finish(_current_status(plugin_name, session))

        for action, state in (("enable", "enabled"), ("disable", "disabled")):
            if arp.query(action, None) is None:
                continue
            global_, group_id, user_id, adapter = _option_values(arp, action)
            await _ensure_scope_permission(
                bot,
                event,
                session,
                global_=global_,
                group_id=group_id,
                user_id=user_id,
                adapter=adapter,
            )
            message = _set_state(
                _plugin_arg(arp, action),
                state,  # type: ignore[arg-type]
                session,
                global_=global_,
                group_id=group_id,
                user_id=user_id,
                adapter=adapter,
            )
            await plugin_cmd.finish(message)
    except ValueError as e:
        await plugin_cmd.finish(str(e))

    await plugin_cmd.finish("Unknown plugin command.")
