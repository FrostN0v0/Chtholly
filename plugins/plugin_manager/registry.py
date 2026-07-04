from __future__ import annotations

from nonebot import get_loaded_plugins
from nonebot.matcher import Matcher  # noqa: TC002

SELF_PLUGIN_NAMES = {"plugin_manager", "plugins.plugin_manager"}


def canonical_plugin_names() -> dict[str, str]:
    names: dict[str, str] = {}
    for plugin in get_loaded_plugins():
        names[plugin.name] = plugin.name
        names[plugin.id_] = plugin.name
        names[plugin.module_name] = plugin.name
    return names


def get_canonical_plugin_names() -> set[str]:
    return {plugin.name for plugin in get_loaded_plugins()}


def get_loaded_plugin_names() -> set[str]:
    return set(canonical_plugin_names())


def resolve_user_plugin_name(value: str) -> str | None:
    return canonical_plugin_names().get(value)


def resolve_matcher_plugin(matcher: type[Matcher]) -> str | None:
    if plugin := getattr(matcher, "plugin", None):
        return getattr(plugin, "name", None)
    if plugin_name := getattr(matcher, "plugin_name", None):
        return plugin_name
    source = getattr(matcher, "_source", None)
    if source and source.plugin_name:
        return source.plugin_name
    return None
