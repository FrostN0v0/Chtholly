from __future__ import annotations

from pathlib import Path
from threading import RLock
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.scalarstring import DoubleQuotedScalarString

from .models import PluginState, PluginSwitch, SwitchConfig, normalize_state

CONFIG_FILE = Path.cwd() / "config" / "plugin-switch.yml"

_yaml = YAML(typ="rt")
_yaml.preserve_quotes = True
_yaml.indent(mapping=2, sequence=4, offset=2)


def _quoted_key(value: str) -> DoubleQuotedScalarString:
    return DoubleQuotedScalarString(str(value))


def _empty_raw() -> CommentedMap:
    raw = CommentedMap()
    raw["plugins"] = CommentedMap()
    return raw


def _as_mapping(value: object, *, field_name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be a mapping")
    return value


def _parse_state_map(
    value: object,
    *,
    field_name: str,
) -> dict[str, dict[str, PluginState]]:
    result: dict[str, dict[str, PluginState]] = {}
    for adapter, entries in _as_mapping(value, field_name=field_name).items():
        adapter_key = str(adapter)
        result[adapter_key] = {}
        entries_map = _as_mapping(entries, field_name=f"{field_name}.{adapter_key}")
        for target_id, state in entries_map.items():
            result[adapter_key][str(target_id)] = normalize_state(
                state,
                field_name=f"{field_name}.{adapter_key}.{target_id}",
            )
    return result


def _parse_config(raw: dict[str, Any]) -> SwitchConfig:
    plugins = _as_mapping(raw.get("plugins"), field_name="plugins")
    parsed = SwitchConfig()
    for plugin_name, value in plugins.items():
        data = _as_mapping(value, field_name=f"plugins.{plugin_name}")
        parsed.plugins[str(plugin_name)] = PluginSwitch(
            default=normalize_state(
                data.get("default"),
                field_name=f"plugins.{plugin_name}.default",
            ),
            global_state=(
                None
                if data.get("global") is None
                else normalize_state(
                    data.get("global"),
                    field_name=f"plugins.{plugin_name}.global",
                )
            ),
            groups=_parse_state_map(
                data.get("groups"),
                field_name=f"plugins.{plugin_name}.groups",
            ),
            users=_parse_state_map(
                data.get("users"),
                field_name=f"plugins.{plugin_name}.users",
            ),
        )
    return parsed


class ConfigStore:
    def __init__(self, config_file: Path = CONFIG_FILE):
        self.config_file = config_file
        self._lock = RLock()
        self._raw: CommentedMap | None = None
        self._config = SwitchConfig()
        self.load()

    def load(self) -> SwitchConfig:
        with self._lock:
            if not self.config_file.exists():
                self.config_file.parent.mkdir(parents=True, exist_ok=True)
                self._raw = _empty_raw()
                self._save_unlocked()
            else:
                with self.config_file.open("r", encoding="utf-8") as file:
                    self._raw = _yaml.load(file) or _empty_raw()
            if not isinstance(self._raw, CommentedMap):
                raise ValueError("plugin-switch.yml root must be a mapping")
            self._raw.setdefault("plugins", CommentedMap())
            self._config = _parse_config(self._raw)
            return self._config

    def get_config(self) -> SwitchConfig:
        with self._lock:
            return self._config

    def save(self) -> None:
        with self._lock:
            self._save_unlocked()
            self.load()

    def _save_unlocked(self) -> None:
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        with self.config_file.open("w", encoding="utf-8") as file:
            _yaml.dump(self._raw or _empty_raw(), file)

    def _plugin_raw(self, plugin_name: str) -> CommentedMap:
        assert self._raw is not None
        plugins = self._raw.setdefault("plugins", CommentedMap())
        plugin = plugins.setdefault(plugin_name, CommentedMap())
        plugin.setdefault("default", "enabled")
        return plugin

    def set_global_state(self, plugin_name: str, state: PluginState) -> None:
        with self._lock:
            self._plugin_raw(plugin_name)["global"] = state
            self.save()

    def set_group_state(
        self,
        plugin_name: str,
        adapter: str,
        group_id: str,
        state: PluginState,
    ) -> None:
        with self._lock:
            plugin = self._plugin_raw(plugin_name)
            groups = plugin.setdefault("groups", CommentedMap())
            adapter_groups = groups.setdefault(adapter, CommentedMap())
            adapter_groups[_quoted_key(group_id)] = state
            self.save()

    def set_user_state(
        self,
        plugin_name: str,
        adapter: str,
        user_id: str,
        state: PluginState,
    ) -> None:
        with self._lock:
            plugin = self._plugin_raw(plugin_name)
            users = plugin.setdefault("users", CommentedMap())
            adapter_users = users.setdefault(adapter, CommentedMap())
            adapter_users[_quoted_key(user_id)] = state
            self.save()

    def resolve_state(
        self,
        plugin_name: str,
        adapter: str,
        *,
        group_id: str | None,
        user_id: str | None,
    ) -> PluginState:
        plugin = self.get_config().plugins.get(plugin_name)
        if plugin is None:
            return "enabled"
        if plugin.global_state == "disabled":
            return "disabled"
        if group_id is not None and (
            state := plugin.groups.get(adapter, {}).get(str(group_id))
        ):
            return state
        if user_id is not None and (
            state := plugin.users.get(adapter, {}).get(str(user_id))
        ):
            return state
        if plugin.global_state == "enabled":
            return "enabled"
        return plugin.default


store = ConfigStore()


def load_config() -> SwitchConfig:
    return store.load()


def get_config() -> SwitchConfig:
    return store.get_config()


def set_global_state(plugin_name: str, state: PluginState) -> None:
    store.set_global_state(plugin_name, state)


def set_group_state(
    plugin_name: str,
    adapter: str,
    group_id: str,
    state: PluginState,
) -> None:
    store.set_group_state(plugin_name, adapter, group_id, state)


def set_user_state(
    plugin_name: str,
    adapter: str,
    user_id: str,
    state: PluginState,
) -> None:
    store.set_user_state(plugin_name, adapter, user_id, state)


def resolve_state(
    plugin_name: str,
    adapter: str,
    *,
    group_id: str | None,
    user_id: str | None,
) -> PluginState:
    return store.resolve_state(plugin_name, adapter, group_id=group_id, user_id=user_id)
