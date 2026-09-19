"""Read-only checks for prerequisites that otherwise fail after plugin imports."""

from __future__ import annotations

import os
import sys
from typing import TypeGuard
from pathlib import Path, PureWindowsPath
import ipaddress
from dataclasses import dataclass
from urllib.parse import unquote, parse_qs, urlsplit
from collections.abc import Mapping
from importlib.metadata import PackageNotFoundError, version

from .configuration import StartupConfig

# These are startup import/service dependencies, not optional tool capabilities.
_DEPENDENCIES = {
    "user": ("database",),
    "webui": ("server", "database"),
    "channel_perception": ("database", "user"),
    "llm_chat": ("llm", "channel_perception", "database", "user", "browser", "htmlrender", "webui"),
    "plugin_workshop": ("webui", "server"),
    "help_menu": ("browser",),
    "status": ("browser",),
    "webui_sso": ("webui", "server"),
    "webui_config_apply": ("webui", "server"),
}
_DISTRIBUTIONS = {
    name: "entari-plugin-" + name for name in ("database", "user", "server", "webui", "llm", "browser", "htmlrender")
}
_SECRET_FIELDS = frozenset(
    {"api_key", "password", "token", "access_token", "secret", "client_secret", "cookie", "cred"}
)


@dataclass(frozen=True)
class ValidationResult:
    issues: tuple[str, ...]
    directories: tuple[Path, ...]
    notes: tuple[str, ...]


def python_issues() -> list[str]:
    return [] if sys.version_info[:2] == (3, 10) else ["Use Python 3.10: run uv sync --locked --python 3.10."]


def _nonempty(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and bool(value.strip())


def _http_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.hostname) and parsed.port != 0
    except ValueError:
        return False


def _has_credentials(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(
            (isinstance(key, str) and (key in _SECRET_FIELDS or key.endswith("_api_key")) and bool(child))
            or (key == "secrets" and bool(child))
            or _has_credentials(child)
            for key, child in value.items()
        )
    return isinstance(value, list) and any(_has_credentials(child) for child in value)


def _models(config: StartupConfig, issues: list[str]) -> None:
    llm = config.plugins.get("llm")
    if llm is None:
        return
    models = llm.get("models", [])
    if not isinstance(models, list) or not models:
        issues.append("llm.models must contain at least one named model, or disable llm and its dependents.")
        return
    identifiers: dict[str, Mapping[str, object]] = {}
    first: Mapping[str, object] | None = None
    for index, model in enumerate(models):
        label = f"llm.models[{index}]"
        if not isinstance(model, Mapping) or not _nonempty(model.get("name")):
            issues.append(f"{label}.name must be a nonempty model identifier.")
            continue
        if first is None:
            first = model
        for field in ("name", "alias"):
            identifier = model.get(field)
            if identifier is None or identifier == "":
                continue
            if not _nonempty(identifier):
                issues.append(f"{label}.{field} must be a nonempty string when configured.")
            elif identifier in identifiers and identifiers[identifier] is not model:
                issues.append(f"{label}.{field} duplicates another model name or alias.")
            else:
                identifiers[identifier] = model
        if model.get("base_url") not in (None, "") and not _http_url(model["base_url"]):
            issues.append(f"{label}.base_url must be an HTTP(S) URL when configured.")
    if llm.get("base_url") not in (None, "") and not _http_url(llm["base_url"]):
        issues.append("llm.base_url must be an HTTP(S) URL.")
    selected: list[Mapping[str, object]] = [first] if first is not None else []
    chat = config.plugins.get("llm_chat", {})
    for field in ("model", "eval_model", "image_generation_model", "image_tag_model"):
        if field == "image_tag_model" and chat.get("image_tags_enabled") is False:
            continue
        identifier = chat.get(field)
        if identifier in (None, ""):
            continue
        if not isinstance(identifier, str) or identifier not in identifiers:
            issues.append(f"llm_chat.{field} must match a configured llm model name or alias.")
        elif all(identifiers[identifier] is not model for model in selected):
            selected.append(identifiers[identifier])
    # Custom gateways and local providers can be unauthenticated. Only require
    # credentials where a selected model targets a known public provider.
    provider_env = {"api.openai.com": "OPENAI_API_KEY", "api.anthropic.com": "ANTHROPIC_API_KEY"}
    for model in selected:
        base = model.get("base_url") or llm.get("base_url") or "https://api.openai.com/v1"
        if not _http_url(base):
            continue
        host = urlsplit(str(base)).hostname
        model_name = str(model["name"])
        provider = model_name.partition("/")[0]
        if provider not in {"openai", "anthropic"} and "/" in model_name:
            continue
        key = provider_env.get(host or "")
        if key and not (_nonempty(model.get("api_key")) or _nonempty(llm.get("api_key")) or os.environ.get(key)):
            issues.append(
                "A default/selected public-provider model requires its api_key or provider environment credential."
            )
            break


def _adapters(config: StartupConfig, issues: list[str]) -> None:
    if "server" not in config.plugins:
        return
    host = config.plugins["server"].get("host", "127.0.0.1")
    try:
        loopback = host == "localhost" or (isinstance(host, str) and ipaddress.ip_address(host).is_loopback)
    except ValueError:
        loopback = False
    if not loopback:
        issues.append("server.host must be loopback; use an authenticated gateway or SSH tunnel for remote access.")
    for index, adapter in enumerate(config.adapters):
        path = adapter.get("$path")
        label = f"server.adapters[{index}]"
        if not _nonempty(path):
            issues.append(f"{label} requires an adapter $path; remove unused adapters.")
            continue
        module = str(path).split(":", 1)[0].strip().replace("@.", "satori.adapters.")
        if module.startswith("@"):
            module = "satori.adapters." + module[1:]
        distribution = None
        if module == "satori.adapters.qq" or module.startswith("satori.adapters.qq."):
            distribution = "satori-python-adapter-qq"
            if "webhook" in str(path).lower() or "secrets" in adapter:
                secrets = adapter.get("secrets")
                if (
                    not isinstance(secrets, Mapping)
                    or not secrets
                    or any(not _nonempty(key) or not _nonempty(value) for key, value in secrets.items())
                ):
                    issues.append(f"{label}.secrets requires nonempty QQ application IDs and secrets.")
            else:
                for field in ("app_id", "secret"):
                    if not _nonempty(adapter.get(field)):
                        issues.append(f"{label}.{field} is required for the enabled QQ adapter.")
        elif module == "satori.adapters.onebot11" or module.startswith("satori.adapters.onebot11."):
            distribution = "satori-python-adapter-onebot11"
            if not _nonempty(adapter.get("access_token")):
                issues.append(f"{label}.access_token is required; configure the same token in the OneBot peer.")
        if distribution is not None:
            try:
                version(distribution)
            except PackageNotFoundError:
                issues.append(f"{label}: adapter package is missing; run uv sync --locked --all-extras.")


def _database(config: StartupConfig, issues: list[str], notes: list[str]) -> tuple[Path, ...]:
    database = config.plugins.get("database")
    if database is None:
        return ()
    entries: list[tuple[str, Mapping[str, object]]] = [("database", database)]
    binds = database.get("binds", {})
    if not isinstance(binds, Mapping):
        issues.append("database.binds must be an object of database configurations.")
    else:
        for index, bind in enumerate(binds.values()):
            if not isinstance(bind, Mapping):
                issues.append(f"database.binds[{index}] must be a database configuration object.")
            else:
                entries.append((f"database.binds[{index}]", bind))
    directories: list[Path] = []
    for label, entry in entries:
        if entry.get("type", "sqlite") != "sqlite":
            continue
        name = entry.get("name", "data.db")
        if not isinstance(name, str) or not name.strip():
            issues.append(f"{label}.name must be a nonempty SQLite filename or :memory:.")
            continue
        if name == ":memory:":
            continue
        readonly = False
        if name.startswith("file:"):
            query = entry.get("query", {})
            options = entry.get("options", {})
            connect_args = options.get("connect_args", {}) if isinstance(options, Mapping) else {}
            uri = (isinstance(query, Mapping) and str(query.get("uri", "")).lower() == "true") or (
                isinstance(connect_args, Mapping) and connect_args.get("uri") is True
            )
            if uri:
                parsed = urlsplit(name)
                params = parse_qs(parsed.query)
                mode = params.get("mode", [query.get("mode") if isinstance(query, Mapping) else None])[0]
                if mode == "memory" or parsed.path == ":memory:":
                    continue
                readonly = mode in {"ro", "rw"}
                name = unquote(parsed.path)
                if os.name == "nt" and name.startswith("/") and len(name) > 2 and name[2] == ":":
                    name = name[1:]
                if not name or parsed.netloc:
                    issues.append(f"{label}.name uses an unsupported SQLite URI; use an explicit local filename.")
                    continue
        if os.name != "nt" and PureWindowsPath(name).drive:
            issues.append(f"{label}.name is a Windows-specific path; set an explicit path for this operating system.")
            continue
        target = config.root / name
        try:
            if target.exists() and not target.is_file():
                issues.append(f"{label}.name must refer to a file, not a directory.")
                continue
            if readonly and not target.is_file():
                issues.append(f"{label}.name uses a non-creating SQLite URI but the database does not exist.")
                continue
            parent = target.parent
            ancestor = parent
            while not ancestor.exists() and ancestor.parent != ancestor:
                ancestor = ancestor.parent
            if not ancestor.is_dir() or not os.access(ancestor, os.W_OK | os.X_OK):
                issues.append(f"{label}.name requires a writable parent directory.")
            elif target.exists() and not os.access(target, os.W_OK):
                issues.append(f"{label}.name refers to a database that is not writable.")
            elif not parent.exists() and parent not in directories:
                directories.append(parent)
                notes.append(f"{label}: parent directory will be initialized by --prepare or normal startup.")
        except (OSError, ValueError):
            issues.append(f"{label}.name cannot be inspected; check the path and filesystem permissions.")
    return tuple(directories)


def validate_configuration(config: StartupConfig) -> ValidationResult:
    issues = python_issues()
    notes: list[str] = []
    plugins = config.plugins
    for name, dependencies in _DEPENDENCIES.items():
        if name in plugins:
            for dependency in dependencies:
                if dependency not in plugins:
                    issues.append(f"Enabled {name} requires enabled {dependency}; configure it or disable {name}.")
    for name, distribution in _DISTRIBUTIONS.items():
        if name in plugins:
            try:
                version(distribution)
            except PackageNotFoundError:
                issues.append(f"Enabled {name} requires its installed package; run uv sync --locked.")
    _models(config, issues)
    _adapters(config, issues)
    if "plugin_workshop" in plugins and not _nonempty(plugins.get("webui", {}).get("password")):
        issues.append("plugin_workshop requires an explicitly configured nonempty webui.password.")
    if plugins.get("browser", {}).get("auto_download_browser") is True:
        issues.append("Set browser.auto_download_browser to false and install resources explicitly with --prepare.")
    if "merged_forward_max_described_images" in plugins.get("llm_chat", {}):
        issues.append(
            "Retired llm_chat.merged_forward_max_described_images: review scripts/migrate_image_input_config.py "
            "against the selected configuration before starting; migration is never automatic."
        )
    tts = plugins.get("tts_service")
    if tts is not None:
        provider = tts.get("provider", "gpt-sovits")
        if provider == "gpt-sovits":
            if not _http_url(tts.get("gpt_sovits_base_url", "http://127.0.0.1:9874")):
                issues.append("tts_service.gpt_sovits_base_url must be an HTTP(S) URL.")
        elif provider == "fish-audio":
            if not _nonempty(tts.get("fish_api_key")):
                issues.append("tts_service.fish_api_key is required for the selected fish-audio provider.")
            if not _http_url(tts.get("fish_api_url", "https://api.fish.audio/v1/tts")):
                issues.append("tts_service.fish_api_url must be an HTTP(S) URL.")
            if not _nonempty(tts.get("fish_model", "s2-pro")):
                issues.append("tts_service.fish_model must be nonempty for the selected fish-audio provider.")
        else:
            issues.append("tts_service.provider must select gpt-sovits or fish-audio.")
    basic = config.native.basic
    if _has_credentials(config.native.data):
        level = basic.log.level
        verbose = isinstance(level, str) and level.upper() in {"DEBUG", "TRACE"}
        verbose = verbose or (isinstance(level, int) and level <= 10)
        if verbose or basic.log.rich_error:
            issues.append(
                "Credential-bearing configurations require INFO-or-higher logging and basic.log.rich_error=false."
            )
    directories = _database(config, issues, notes)
    return ValidationResult(tuple(issues), directories, tuple(notes))


def initialize_directories(result: ValidationResult) -> None:
    """Only create missing parents after every prerequisite has passed."""
    if result.issues:
        raise ValueError("Cannot initialize directories for invalid configuration.")
    for directory in result.directories:
        directory.mkdir(parents=True, exist_ok=True)
