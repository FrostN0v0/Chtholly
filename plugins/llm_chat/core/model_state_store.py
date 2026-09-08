"""Atomic file adapter for persisted entari-plugin-llm model selections."""

from __future__ import annotations

import os
import json
from pathlib import Path
from tempfile import NamedTemporaryFile
from collections.abc import Sequence

from .model_state import ConfiguredModel, ModelStateRepair, repair_model_state


def repair_model_state_file(path: Path, models: Sequence[ConfiguredModel]) -> ModelStateRepair:
    if not models:
        return ModelStateRepair(fallback_model=None)

    raw_data: object = {}
    if path.exists():
        try:
            raw_data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid state JSON at {path}") from error
    if not isinstance(raw_data, dict):
        raise ValueError(f"state root is not an object at {path}")

    repaired_data, repair = repair_model_state(raw_data, models)
    if repair.changed:
        _write_json_atomic(path, repaired_data)
    return repair


def _write_json_atomic(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(data, temporary, ensure_ascii=False, indent=2)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
