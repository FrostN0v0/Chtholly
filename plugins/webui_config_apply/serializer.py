"""Local, disposable replacement for Entari's positional interpolation dumper."""

from __future__ import annotations

import os
from typing import Any
from pathlib import Path
from collections.abc import Mapping, Callable

from arclet.entari.config import file as config_file
from arclet.entari.config.file import EntariConfig

from utils.webui_config_core import ConfigValidationError, validate_config, restore_environment_templates


def read_source(path: Path) -> tuple[bytes, dict[str, Any]]:
    loader = config_file._loaders.get(path.suffix.lstrip("."))
    if loader is None:
        raise ConfigValidationError("The configuration source format is not supported", code="unsupported_format")
    content = path.read_bytes()
    try:
        source = loader(content.decode("utf-8-sig"))
    except Exception:
        raise ConfigValidationError("The saved configuration could not be decoded", code="invalid_document") from None
    if not isinstance(source, dict):
        raise ConfigValidationError("The configuration source must be an object")
    return content, source


def render_source(
    cfg: EntariConfig,
    path: Path,
    save_path: Path,
    data: Mapping[str, Any],
    indent: int,
    apply_schema: bool,
    *,
    source: Mapping[str, Any] | None = None,
) -> bytes:
    if source is None:
        _, source = read_source(path)
    # Never call loader(): it interpolates and appends stale _records. Registry
    # loaders preserve the current source, including the optional outer wrapper.
    origin = dict(source)
    if "entari" in origin:
        origin["entari"] = dict(data)
    else:
        origin = dict(data)
    restored = restore_environment_templates(origin, source, cfg.env_vars)
    if not isinstance(restored, dict):
        raise ConfigValidationError("Configuration must be an object")
    validate_config(restored, cfg.env_vars)
    dumper = config_file._dumpers.get(save_path.suffix.lstrip("."))
    if dumper is None:
        raise ConfigValidationError("The configuration output format is not supported", code="unsupported_format")
    schema_file = f"{save_path.stem}.schema.json" if apply_schema else None
    try:
        text, _ = dumper(restored, indent, schema_file)
        return text.encode("utf-8")
    except Exception:
        raise ConfigValidationError("The configuration could not be serialized", code="serialization_failed") from None


def install_dumper() -> Callable[[], None]:
    original = EntariConfig.dumper

    def safe_dumper(self: EntariConfig, path: Path, save_path: Path, data: dict, indent: int, apply_schema: bool):
        if not path.exists():
            return
        content = render_source(self, path, save_path, data, indent, apply_schema)
        with save_path.open("wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())

    EntariConfig.dumper = safe_dumper

    def dispose() -> None:
        if EntariConfig.dumper is safe_dumper:
            EntariConfig.dumper = original

    return dispose
