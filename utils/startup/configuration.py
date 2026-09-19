"""Load configuration through Entari while keeping diagnostics value-free."""

from __future__ import annotations

from io import StringIO
import os
from typing import TYPE_CHECKING
from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from collections.abc import Mapping

if TYPE_CHECKING:
    from arclet.entari.config import EntariConfig


class StartupError(ValueError):
    """An actionable diagnostic that never includes configuration values."""


@dataclass(frozen=True)
class StartupConfig:
    native: EntariConfig
    root: Path
    plugins: Mapping[str, Mapping[str, object]]
    adapters: tuple[Mapping[str, object], ...]


def select_config_path(explicit: Path | None, root: Path, environment: Mapping[str, str]) -> Path:
    """Select the source without rewriting it or changing relative-path semantics."""
    if explicit is not None:
        selected = explicit
    elif environment.get("ENTARI_CONFIG_FILE", "").strip():
        selected = Path(environment["ENTARI_CONFIG_FILE"])
    elif (root / "entari.local.yml").is_file():
        selected = Path("entari.local.yml")
    else:
        selected = Path("entari.yml")
    return selected if selected.is_absolute() else root / selected


def canonical_plugin(name: str) -> str:
    if name.startswith("entari_plugin_"):
        return name.removeprefix("entari_plugin_")
    if name.startswith("plugins."):
        return name.removeprefix("plugins.")
    return name


def is_disabled(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        from arclet.entari.filter.parse import evaluate_disable

        return evaluate_disable(value)
    if value is None:
        return False
    raise StartupError("Plugin or adapter $disable must be a boolean or an Entari expression.")


def load_configuration(explicit: Path | None = None) -> StartupConfig:
    """Use native dotenv, interpolation, includes, prefixes and disable expressions."""
    root = Path.cwd()
    try:
        # Parser exceptions and warnings can contain entire credential-bearing lines.
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            from arclet.entari.config import EntariConfig
            from arclet.entari.config.env import load_env_with_environment

            environment = load_env_with_environment()
            path = select_config_path(explicit, root, environment)
            if not path.is_file():
                raise StartupError("Selected configuration is not a readable file; set --config or create entari.yml.")
            native = EntariConfig.load(path)
            plugins: dict[str, Mapping[str, object]] = {}
            for key, values in native.plugin.items():
                if key.startswith("$"):
                    continue
                if not isinstance(values, Mapping):
                    raise StartupError("Each plugin configuration must be an object.")
                if values.get("$optional") or is_disabled(values.get("$disable")):
                    continue
                names = native._plugin_names.get(key, [key])
                for name in names:
                    canonical = canonical_plugin(name)
                    if canonical in plugins:
                        raise StartupError("A plugin is enabled more than once; remove duplicate names or prefixes.")
                    plugins[canonical] = values
            adapters: list[Mapping[str, object]] = []
            if "server" in plugins:
                for entries in (plugins["server"].get("adapters", []), native.data.get("adapters", [])):
                    if not isinstance(entries, list):
                        raise StartupError("Server adapters must be an array of objects.")
                    for entry in entries:
                        if not isinstance(entry, Mapping):
                            raise StartupError("Each server adapter must be an object.")
                        if "$disable" in entry:
                            raise StartupError(
                                "Adapter $disable is unsupported; remove unused adapters from the configuration."
                            )
                        adapters.append(entry)
            return StartupConfig(native, root, plugins, tuple(adapters))
    except StartupError:
        raise
    except Exception:
        raise StartupError(
            "Cannot load configuration; check YAML structure, environment expressions, $files and $disable expressions."
        ) from None


def prepare_runtime_config(config: StartupConfig) -> None:
    """Keep native child-process tooling on the same selected configuration."""
    os.environ["ENTARI_CONFIG_FILE"] = str(config.native.path)
