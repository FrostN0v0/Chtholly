"""Stage native saves on detached config state, then commit validated bytes."""

from __future__ import annotations

from copy import deepcopy
from types import MethodType
from typing import Any, cast
from hashlib import sha256
from pathlib import Path
from threading import RLock
from collections.abc import Mapping, Callable

from tarina.tools import nest_dict_update, nest_list_update
from arclet.entari.plugin import find_plugin, get_plugins
from arclet.entari.config.file import EntariConfig

from utils.webui_config_core import ConfigValidationError, prepare_config, validate_config, validate_candidate
from utils.webui_config_core.validation import same_config

from .serializer import read_source, render_source
from .persistence import persist_candidate
from .model_reload import prepare_model_reload


class SaveError(ValueError):
    def __init__(self, message: str, *, code: str, status: int = 400) -> None:
        self.code = code
        self.status = status
        super().__init__(message)


def _canonical_plugins(raw: Mapping[str, Any]) -> dict[str, Any]:
    plugins: dict[str, Any] = {}
    for raw_key, raw_value in raw.items():
        key = raw_key
        value = deepcopy(raw_value)
        if not isinstance(key, str):
            raise ConfigValidationError("Plugin keys must be strings", path="config.plugins")
        if not key.startswith("$"):
            if not isinstance(value, dict):
                raise ConfigValidationError("Plugin configuration must be an object", path="config.plugins")
            if key.startswith("~"):
                key = key[1:]
                if "$disable" not in value or isinstance(value["$disable"], bool):
                    value["$disable"] = True
            elif key.startswith("?"):
                key = key[1:]
                value["$optional"] = True
        if key in plugins:
            raise ConfigValidationError("Plugin keys must be unique", path="config.plugins")
        plugins[key] = value
    return plugins


def _current_clone(live: EntariConfig) -> tuple[EntariConfig, dict[Path, tuple[bytes, dict[str, Any]]]]:
    content, raw = read_source(live.path)
    source = raw.get("entari", raw)
    if not isinstance(source, Mapping) or not isinstance(source.get("plugins", {}), Mapping):
        raise ConfigValidationError("Saved configuration must contain an object of plugins")
    # __post_init__ would replace EntariConfig.instance. deepcopy never runs it.
    clone = deepcopy(live)
    clone._origin_data = deepcopy(dict(source))
    clone.plugin = _canonical_plugins(source.get("plugins", {}))
    clone._origin_data["plugins"] = clone.plugin
    clone._records = {}
    files = source.get("plugins", {}).get("$files", [])
    if not isinstance(files, list) or any(not isinstance(item, str) for item in files):
        raise ConfigValidationError("Plugin files must be an array of paths", path="config.plugins.$files")
    clone.plugin_extra_files = deepcopy(files)
    snapshots = {live.path: (content, raw)}
    for filename in files:
        path = Path(filename)
        if path.is_dir():
            paths = [entry for entry in path.iterdir() if entry.is_file() and not entry.name.endswith(".schema.json")]
        elif path.is_file() and not path.name.endswith(".schema.json"):
            paths = [path]
        else:
            raise ConfigValidationError("A configured plugin file is unavailable", code="source_unavailable")
        for extra_path in paths:
            extra_bytes, extra_raw = read_source(extra_path)
            snapshots[extra_path] = (extra_bytes, extra_raw)
            clone.plugin[extra_path.stem] = deepcopy(extra_raw.get("entari", extra_raw))
    return clone, snapshots


def _plugin_key(plugin_id: str) -> str:
    plug = find_plugin(plugin_id)
    if plug is None:
        raise SaveError("Plugin not found", code="plugin_not_found", status=404)
    return getattr(plug, "_config_key", plugin_id)


def _replace_plugin(clone: EntariConfig, key: str, value: Any) -> None:
    if not isinstance(value, Mapping):
        raise ConfigValidationError("Plugin configuration must be an object", path="config.plugins")
    clone.plugin[key] = deepcopy(dict(value))


def _update_section(clone: EntariConfig, section: str, value: Any) -> None:
    if section == "basic":
        if not isinstance(value, dict):
            raise ConfigValidationError("Basic configuration must be an object", path="config.basic")
        current = clone.data.setdefault("basic", {})
        nest_dict_update(current, deepcopy(value))
    elif section == "adapters":
        if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
            raise ConfigValidationError("Adapters must be an array of objects", path="config.adapters")
        nest_list_update(clone.data.setdefault("adapters", []), deepcopy(value))
    elif section == "plugins":
        if not isinstance(value, Mapping):
            raise ConfigValidationError("Plugins must be an object", path="config.plugins")
        # The native endpoint accepts partial sections. Replace only supplied
        # plugin objects; merging a model by index resurrects removed overrides.
        for key, config in _canonical_plugins(value).items():
            clone.plugin[key] = config
    elif section.startswith("plugins:"):
        key = section[len("plugins:") :]
        plugin_keys = {plug.id: plug._config_key for plug in get_plugins()}
        _replace_plugin(clone, plugin_keys.get(key, key), value)
    else:
        raise SaveError("Configuration section not found", code="config_section_not_found", status=404)


class ConfigSaver:
    def __init__(self, running_sha256: str | None, restart_available: Callable[[], bool]) -> None:
        self.running_sha256 = running_sha256
        self.restart_available = restart_available
        self.lock = RLock()
        self.application_mode = "restart"
        self.last_application: dict[str, object] = {"result": "applied" if running_sha256 else "unverified"}
        live = EntariConfig.instance
        content = live.path.read_bytes()
        self._running_source = (
            validate_candidate(content, live.env_vars) if sha256(content).hexdigest() == running_sha256 else None
        )

    def save_plugin(self, plugin_id: str, value: Mapping[str, Any]) -> dict[str, Any]:
        return self._save(plugin_key=_plugin_key(plugin_id), value=value)

    def save_section(self, section: str, value: Any) -> dict[str, Any]:
        return self._save(section=section, value=value)

    def _save(self, *, value: Any, plugin_key: str | None = None, section: str | None = None) -> dict[str, Any]:
        with self.lock:
            live = EntariConfig.instance
            clone, snapshots = _current_clone(live)
            original = deepcopy(clone.data)
            if plugin_key is not None:
                _replace_plugin(clone, plugin_key, value)
            elif section is not None:
                _update_section(clone, section, value)
            prepared = prepare_config(clone.data, original, live.env_vars)
            clone._origin_data = prepared
            clone.plugin = cast(dict[str, dict[str, object]], prepared["plugins"])
            staged: dict[Path, bytes] = {}

            def stage(self: EntariConfig, path: Path, save_path: Path, data: dict, indent: int, apply_schema: bool):
                if path not in snapshots or save_path != path:
                    raise SaveError("The save target changed; reload the page", code="source_changed", status=409)
                content = render_source(self, path, save_path, data, indent, apply_schema, source=snapshots[path][1])
                if save_path.suffix.lower() in {".yaml", ".yml", ".json"}:
                    validate_candidate(content, live.env_vars)
                else:
                    validate_config(data, live.env_vars)
                staged[save_path] = content

            clone.dumper = MethodType(stage, clone)
            clone.save(apply_schema=live.basic.schema)
            candidate = staged[live.path]
            changed = {path: content for path, content in staged.items() if content != snapshots[path][0]}
            if any(path != live.path for path in changed):
                raise SaveError(
                    "Linked plugin files need controlled deployment; this helper watches only the main configuration",
                    code="external_config_change",
                    status=409,
                )
            source = validate_candidate(candidate, live.env_vars)
            digest = sha256(candidate).hexdigest()
            unchanged = digest == self.running_sha256 or same_config(source, self._running_source)
            model_reload = None
            if not unchanged and self._running_source is not None and not clone.plugin_extra_files:
                model_reload = prepare_model_reload(self._running_source, source, live.env_vars)
            mode = "unchanged" if unchanged else "hot_reload" if model_reload is not None else "restart"
            if mode == "restart" and not self.restart_available():
                self.last_application = {"result": "rejected", "candidate_sha256": digest, "mode": mode}
                raise SaveError(
                    "This configuration change requires the managed restart helper",
                    code="helper_unavailable",
                    status=503,
                )
            try:
                with persist_candidate(live, clone, snapshots, changed):
                    if model_reload is not None:
                        model_reload.apply()
            except Exception as exc:
                recovery_failed = isinstance(exc, ConfigValidationError) and exc.code == "config_rollback_failed"
                self.last_application = {
                    "result": "rollback_failed" if recovery_failed else "rejected",
                    "candidate_sha256": digest,
                    "mode": mode,
                }
                if recovery_failed:
                    self.running_sha256 = None
                    self._running_source = None
                if isinstance(exc, (ConfigValidationError, SaveError)):
                    raise
                raise SaveError(
                    "Configuration application failed; the previous saved configuration was restored",
                    code="config_apply_failed",
                    status=503,
                ) from None
            if mode != "restart":
                self.running_sha256 = digest
                self._running_source = deepcopy(dict(source))
            self.application_mode = mode
            self.last_application = {
                "result": "pending" if mode == "restart" else "applied",
                "candidate_sha256": digest,
                "mode": mode,
            }
            return {
                "success": True,
                "candidate_sha256": digest,
                "restart_required": mode == "restart",
                "application_mode": mode,
                "applied": mode != "restart",
            }
