"""Pure data assembly for the help menu: plugins -> MenuEntry groups."""

from dataclasses import field, dataclass
from collections.abc import Mapping

from arclet.entari.plugin import get_plugins as _get_plugins
from arclet.entari.plugin.model import Plugin, PluginRole

DEFAULT_ICON = "🧩"  # puzzle piece
DEFAULT_CATEGORY = "其他"

HIDDEN_ROLES = {PluginRole.LIBRARY, PluginRole.UTILITY}
HIDDEN_ID_PREFIXES = ("entari.plugin.", ".")


@dataclass(slots=True, frozen=True)
class MenuEntry:
    name: str
    version: str | None
    description: str | None
    icon: str
    category: str
    commands: list[str] = field(default_factory=list)


def _plugin_commands(plug: Plugin) -> list[str]:
    """Render `(prefixes, command)` pairs from plugin extras into display strings."""
    result: list[str] = []
    extra = getattr(plug, "_extra", {})
    if not isinstance(extra, dict):
        return result
    commands = extra.get("commands", [])
    if not isinstance(commands, (list, tuple)):
        return result
    for entry in commands:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            continue
        prefixes, cmd = entry
        if not isinstance(cmd, str):
            continue
        if isinstance(prefixes, (list, tuple)) and all(isinstance(prefix, str) for prefix in prefixes):
            result.append(f"{'|'.join(prefixes)}{cmd}")
        elif isinstance(prefixes, str):
            result.append(f"{prefixes}{cmd}")
        elif not prefixes:
            result.append(cmd)
    return result


def _help_meta(plug: Plugin) -> dict[str, str]:
    """Read the optional module-level HELP_META dict from the plugin module."""
    raw = getattr(plug.module, "HELP_META", None)
    if not isinstance(raw, Mapping):
        return {}
    return {key: value for key, value in raw.items() if isinstance(key, str) and isinstance(value, str)}


def collect_entries(
    *,
    show_hidden: bool = False,
    custom_icons: dict[str, str] | None = None,
) -> dict[str, list[MenuEntry]]:
    """Group loaded plugins into {category: [MenuEntry]} for rendering."""
    custom_icons = custom_icons or {}
    grouped: dict[str, list[MenuEntry]] = {}

    for plug in _get_plugins():
        meta = plug.metadata
        if meta is None:
            continue
        if not show_hidden:
            if meta.role in HIDDEN_ROLES:
                continue
            if plug.id.startswith(HIDDEN_ID_PREFIXES):
                continue

        help_meta = _help_meta(plug)
        name = meta.name or plug.id
        icon = custom_icons.get(name) or custom_icons.get(plug.id) or help_meta.get("icon") or meta.icon or DEFAULT_ICON
        category = help_meta.get("category", DEFAULT_CATEGORY)

        entry = MenuEntry(
            name=name,
            version=meta.version,
            description=meta.description,
            icon=icon,
            category=category,
            commands=_plugin_commands(plug),
        )
        grouped.setdefault(category, []).append(entry)

    for entries in grouped.values():
        entries.sort(key=lambda entry: entry.name)
    return dict(sorted(grouped.items(), key=lambda item: (item[0] == DEFAULT_CATEGORY, item[0])))
