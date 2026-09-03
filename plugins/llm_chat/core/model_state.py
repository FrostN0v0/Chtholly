"""Pure repair logic for persisted entari-plugin-llm model selections."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from collections.abc import Mapping, Sequence

_STATE_KEYS = frozenset({"default_model", "current_sessions", "session_models"})


@dataclass(frozen=True, slots=True)
class ConfiguredModel:
    name: str
    alias: str | None = None


@dataclass(frozen=True, slots=True)
class ModelStateRepair:
    fallback_model: str | None
    default_model_updates: int = 0
    session_model_updates: int = 0

    @property
    def changed(self) -> bool:
        return self.default_model_updates > 0 or self.session_model_updates > 0


def repair_model_state(
    raw_data: Mapping[str, object],
    models: Sequence[ConfiguredModel],
) -> tuple[dict[str, object], ModelStateRepair]:
    """Canonicalize persisted model selections and replace removed models."""
    data = deepcopy(dict(raw_data))
    canonical_names, first_model = _canonical_model_names(models)
    if first_model is None:
        return data, ModelStateRepair(fallback_model=None)

    legacy_state = any(key in data for key in _STATE_KEYS)
    global_state = data if legacy_state else data.get("$default")
    configured_global = _canonical_model_name(
        global_state.get("default_model") if isinstance(global_state, dict) else None,
        canonical_names,
    )
    fallback_model = configured_global or first_model

    default_updates = 0
    session_updates = 0
    if legacy_state:
        default_updates, session_updates = _repair_scope_state(data, canonical_names, fallback_model)
    else:
        if not isinstance(global_state, dict):
            data["$default"] = {
                "default_model": fallback_model,
                "current_sessions": {},
                "session_models": {},
            }
            default_updates += 1
        for state in data.values():
            if not isinstance(state, dict):
                continue
            repaired_defaults, repaired_sessions = _repair_scope_state(
                state,
                canonical_names,
                fallback_model,
            )
            default_updates += repaired_defaults
            session_updates += repaired_sessions

    return data, ModelStateRepair(
        fallback_model=fallback_model,
        default_model_updates=default_updates,
        session_model_updates=session_updates,
    )


def _canonical_model_names(models: Sequence[ConfiguredModel]) -> tuple[dict[str, str], str | None]:
    canonical_names: dict[str, str] = {}
    first_model: str | None = None
    for model in models:
        name = model.name.strip()
        if not name:
            continue
        if first_model is None:
            first_model = name
        canonical_names.setdefault(name, name)
        if model.alias:
            alias = model.alias.strip()
            if alias:
                canonical_names.setdefault(alias, name)
    return canonical_names, first_model


def _canonical_model_name(value: object, canonical_names: Mapping[str, str]) -> str | None:
    return canonical_names.get(value) if isinstance(value, str) else None


def _repair_scope_state(
    state: dict[str, object],
    canonical_names: Mapping[str, str],
    fallback_model: str,
) -> tuple[int, int]:
    current_default = state.get("default_model")
    repaired_default = _canonical_model_name(current_default, canonical_names) or fallback_model
    default_updates = int(current_default != repaired_default)
    if default_updates:
        state["default_model"] = repaired_default

    session_updates = 0
    session_models = state.get("session_models")
    if isinstance(session_models, dict):
        for session_id, current_model in tuple(session_models.items()):
            repaired_model = _canonical_model_name(current_model, canonical_names) or fallback_model
            if current_model == repaired_model:
                continue
            session_models[session_id] = repaired_model
            session_updates += 1
    return default_updates, session_updates
