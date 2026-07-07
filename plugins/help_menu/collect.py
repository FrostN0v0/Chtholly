"""Pure data assembly for the help menu: plugins -> MenuEntry groups."""

from dataclasses import field, dataclass

from arclet.entari.plugin import Plugin, PluginRole, get_plugins as _get_plugins

DEFAULT_ICON = "\U0001f9e9"  # puzzle piece
DEFAULT_CATEGORY = "其他"

HIDDEN_ROLES = {PluginRole.LIBRARY, PluginRole.UTILITY}
HIDDEN_ID_PREFIXES = ("entari.plugin.", ".")


@dataclass
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
    for prefixes, cmd in plug._extra.get("commands", []):
        if prefixes and all(isinstance(p, str) for p in prefixes):
            result.append(f"{'|'.join(prefixes)}{cmd}")
        else:
            result.append(cmd)
    return result


def _help_meta(plug: Plugin) -> dict[str, str]:
    """Read the optional module-level HELP_META dict from the plugin module."""
    meta = getattr(plug.module, "HELP_META", None)
    return meta if isinstance(meta, dict) else {}


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
        entries.sort(key=lambda e: e.name)
    return dict(sorted(grouped.items(), key=lambda kv: (kv[0] == DEFAULT_CATEGORY, kv[0])))
