"""Stage native field-level configuration edits without publishing runtime state."""

from __future__ import annotations

from copy import deepcopy
from types import MethodType
from typing import Any
from pathlib import Path
from dataclasses import dataclass
from collections.abc import Mapping, Callable

from arclet.entari.config.file import EntariConfig

from utils.webui_config_core import ConfigValidationError, prepare_config, validate_config, validate_candidate

from .serializer import read_source, render_source, canonical_plugins


@dataclass(frozen=True)
class StagedConfig:
    config: EntariConfig
    snapshots: dict[Path, tuple[bytes, dict[str, Any]]]
    changed: dict[Path, bytes]
    content: bytes
    source: Mapping[str, object]


def _current_clone(live: EntariConfig) -> tuple[EntariConfig, dict[Path, tuple[bytes, dict[str, Any]]]]:
    content, raw = read_source(live.path)
    source = raw.get("entari", raw)
    if not isinstance(source, dict) or not isinstance(source.get("plugins", {}), Mapping):
        raise ConfigValidationError("Saved configuration must contain an object of plugins")
    clone = deepcopy(live)
    clone._origin_data = deepcopy(source)
    clone.plugin = canonical_plugins(source.get("plugins", {}))
    clone._origin_data["plugins"] = clone.plugin
    clone._root_plugin_keys = {key for key in clone.plugin if not key.startswith("$")}
    clone._extra_stems = {}
    clone._records = {}
    files = source.get("plugins", {}).get("$files", [])
    if not isinstance(files, list) or any(not isinstance(item, str) for item in files):
        raise ConfigValidationError("Plugin files must be an array of paths", path="config.plugins.$files")
    clone.plugin_extra_files = []
    snapshots = {live.path: (content, raw)}
    for filename in files:
        path = Path(filename)
        if not path.is_absolute():
            path = live.path.parent / path
        path = path.resolve()
        clone.plugin_extra_files.append(str(path))
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


def stage_config(
    live: EntariConfig,
    section: str,
    value: object | Callable[[Any], object],
    *,
    mutate: bool,
) -> StagedConfig:
    from entari_plugin_webui.services import config_ops, config_service

    clone, snapshots = _current_clone(live)
    original = deepcopy(clone.data)
    raw = config_service.read_section(clone, section)
    desired = value(deepcopy(raw)) if mutate and callable(value) else value
    desired = {} if desired is None else desired
    desired = config_ops.strip_new_defaults(raw, desired, config_service._effective(section))
    target, _, _ = config_service._target_for(clone, section)
    config_ops.apply_ops(target, config_ops.diff(raw, desired).ops)
    prepared = prepare_config(clone.data, original, live.env_vars)
    # Apply normalization back as a diff so unchanged YAML nodes retain comments and ordering.
    config_ops.apply_ops(clone.data, config_ops.diff(clone.data, prepared).ops)
    staged: dict[Path, bytes] = {}

    def stage(self: EntariConfig, path: Path, save_path: Path, data: dict, indent: int, apply_schema: bool):
        if path not in snapshots or save_path != path:
            raise ConfigValidationError("The save target changed; reload the page", code="source_changed")
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
        raise ConfigValidationError(
            "Linked plugin files need controlled deployment; no configuration was written",
            code="external_config_change",
        )
    return StagedConfig(clone, snapshots, changed, candidate, validate_candidate(candidate, live.env_vars))
