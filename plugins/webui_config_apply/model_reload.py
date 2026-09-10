"""Prepare model-only updates without loading plugins or mutating live state."""

from __future__ import annotations

import sys
from copy import deepcopy
from types import GetSetDescriptorType
from typing import Any, cast, get_args, get_type_hints
from pathlib import Path
from dataclasses import field, fields, dataclass
from collections.abc import Mapping, Callable

from tarina import generic_isinstance

from utils.webui_config_core import ConfigValidationError, validate_config, normalize_model_defaults
from utils.llm_model_core.state import ConfiguredModel
from utils.llm_model_core.state_store import repair_model_state_file
from utils.webui_config_core.templates import TEMPLATE, environment_reference
from utils.webui_config_core.validation import same_config

_MODEL_FIELDS = frozenset({"api_key", "base_url", "prompt", "models"})


@dataclass(frozen=True, repr=False)
class ModelReloadBindings:
    """Explicit runtime dependencies; supplying these never loads an LLM plugin."""

    config_key: str
    plugin: Any
    runtime_config: Any
    config_type: type
    validate: Callable[[type, dict[str, Any]], Any]
    state_path: Path


@dataclass(frozen=True, repr=False)
class ModelReload:
    config_key: str
    _bindings: ModelReloadBindings = field(repr=False)
    _runtime_before: dict[str, Any] = field(repr=False)
    _runtime_after: dict[str, Any] = field(repr=False)
    _runtime_descriptor: GetSetDescriptorType = field(repr=False)
    _plugin_namespace: dict[str, Any] = field(repr=False)
    _plugin_before: Mapping[str, Any] = field(repr=False)
    _plugin_after: dict[str, Any] = field(repr=False)
    _models: tuple[ConfiguredModel, ...] = field(repr=False)

    def apply(self) -> None:
        """Repair the latest state, then publish references with no await or callbacks."""
        binding = self._bindings
        if (
            vars(binding.runtime_config) is not self._runtime_before
            or vars(binding.plugin) is not self._plugin_namespace
            or self._plugin_namespace.get("config") is not self._plugin_before
            or self._plugin_namespace.get("_is_disposed", False)
        ):
            raise ConfigValidationError("The running model configuration changed", code="stale_runtime")
        try:
            repair_model_state_file(binding.state_path, self._models)
        except Exception:
            raise ConfigValidationError(
                "Persisted model selections could not be updated", code="model_state_failed"
            ) from None
        # The descriptor and both exact dictionaries were checked during preparation.
        # All fallible I/O finishes before these non-callback reference publications.
        self._runtime_descriptor.__set__(binding.runtime_config, self._runtime_after)
        self._plugin_namespace["config"] = self._plugin_after


def _loaded_bindings() -> ModelReloadBindings | None:
    module = sys.modules.get("entari_plugin_llm.config")
    entari = sys.modules.get("arclet.entari")
    plugins = sys.modules.get("arclet.entari.plugin")
    config = sys.modules.get("arclet.entari.config")
    if module is None or entari is None or plugins is None or config is None:
        return None
    plug = plugins.find_plugin("entari_plugin_llm")
    if plug is None or plug._is_disposed:
        return None
    config_type = getattr(module, "Config", None)
    runtime = getattr(module, "_conf", None)
    if config_type is None or type(runtime) is not config_type:
        return None
    # get_data_file creates directories, which preparation must never do.
    state_path = entari.local_data._get_base_data_dir() / "entari_plugin_llm" / "state.json"
    return ModelReloadBindings(
        config_key=plug._config_key,
        plugin=plug,
        runtime_config=runtime,
        config_type=config_type,
        validate=config.config_model_validate,
        state_path=state_path,
    )


def _plugin(root: Mapping[str, Any], config_key: str) -> dict[str, Any] | None:
    body = root.get("entari", root)
    if not isinstance(body, Mapping) or not isinstance(body.get("plugins"), Mapping):
        return None
    plugins = body["plugins"]
    keys = [key for key in (config_key, f"?{config_key}") if key in plugins]
    if len(keys) != 1:
        return None
    value = plugins[keys[0]]
    if not isinstance(value, dict) or value.get("$disable"):
        return None
    return value


def _expand(value: Any, env: Mapping[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: _expand(child, env) for key, child in value.items()}
    if isinstance(value, list):
        return [_expand(child, env) for child in value]
    if not isinstance(value, str):
        return deepcopy(value)

    def replace(match) -> str:
        reference = environment_reference(match.group("expression"))
        if reference is None:
            raise ConfigValidationError("Unsupported configuration expression", code="invalid_environment")
        if not reference.optional and not env.get(reference.name):
            raise ConfigValidationError(
                "Referenced environment variable is missing or empty", code="missing_environment"
            )
        # The restoration helper rejects empty interpolations as ambiguous evidence;
        # expansion must instead retain the intentional empty value of env.get(...).
        return reference.resolve(env)

    expanded = TEMPLATE.sub(replace, value)
    if "${{" in expanded:
        raise ConfigValidationError("Malformed configuration expression", code="invalid_environment")
    return expanded


def _validate_model_fields(value: dict[str, Any], config_type: type) -> type:
    """Check only this hot-update surface against the active upstream annotations."""
    annotations = get_type_hints(config_type)
    for name in _MODEL_FIELDS - {"models"}:
        if name in value and not generic_isinstance(value[name], annotations[name]):
            raise ConfigValidationError("Invalid global model field type")
    model_type = get_args(annotations["models"])[0]
    model_annotations = get_type_hints(model_type)
    models = value.get("models")
    if not isinstance(models, list) or not models:
        raise ConfigValidationError("Models must be a nonempty array")
    identifiers: dict[str, int] = {}
    for index, model in enumerate(models):
        if not isinstance(model, dict):
            raise ConfigValidationError("Each model must be an object")
        for name, expected in model_annotations.items():
            if name in model and not generic_isinstance(model[name], expected):
                raise ConfigValidationError("Invalid scoped model field type")
        for name in ("name", "alias"):
            identifier = model.get(name)
            if identifier is None and name == "alias":
                continue
            if not isinstance(identifier, str) or (name == "name" and not identifier):
                raise ConfigValidationError("Model identifiers must be nonempty strings")
            if identifier != identifier.strip():
                raise ConfigValidationError("Model identifiers cannot have surrounding whitespace")
            if not identifier:
                continue
            if identifier in identifiers and identifiers[identifier] != index:
                raise ConfigValidationError("Model names and aliases must be unique", code="duplicate_model")
            identifiers[identifier] = index
    return model_type


def prepare_model_reload(
    before_root: Mapping[str, Any],
    after_root: Mapping[str, Any],
    env_vars: Mapping[str, str],
    *,
    bindings: ModelReloadBindings | None = None,
) -> ModelReload | None:
    """Validate and detach an eligible candidate; None requires managed restart.

    Only changes to one already-active LLM plugin's model fields are supported.
    The caller owns the synchronous save transaction and running baseline.
    """
    validate_config(after_root, env_vars)
    try:
        binding = bindings if bindings is not None else _loaded_bindings()
        if binding is None:
            return None
        before = normalize_model_defaults(before_root)
        after = normalize_model_defaults(after_root)
        old_plugin = _plugin(before, binding.config_key)
        new_plugin = _plugin(after, binding.config_key)
        if old_plugin is None or new_plugin is None or same_config(before, after):
            return None
        candidate = deepcopy(new_plugin)
        for name in _MODEL_FIELDS:
            old_plugin.pop(name, None)
            new_plugin.pop(name, None)
        if not same_config(before, after):
            return None
        expanded = _expand(candidate, env_vars)
        # An optional environment reference can expand to an inherited placeholder.
        normalized = normalize_model_defaults({"plugins": {"entari_plugin_llm": expanded}})
        normalized_plugins = cast(dict[str, dict[str, Any]], normalized["plugins"])
        expanded = normalized_plugins["entari_plugin_llm"]
        model_type = _validate_model_fields(expanded, binding.config_type)
        declared_model_fields = {item.name for item in fields(model_type)}
        old_candidate = _plugin(before_root, binding.config_key)
        if old_candidate is None:
            return None
        # Unknown model settings have no guaranteed runtime semantics. Do not hide
        # their changes inside the otherwise supported replacement of models.
        for config in (old_candidate, candidate):
            if any(
                not isinstance(model, dict) or not model.keys() <= declared_model_fields
                for model in config.get("models", [])
            ):
                return None
        fresh = binding.validate(binding.config_type, deepcopy(expanded))
        if type(fresh) is not binding.config_type or type(binding.runtime_config) is not binding.config_type:
            return None
        runtime_before = vars(binding.runtime_config)
        runtime_after = vars(fresh)
        plugin_namespace = vars(binding.plugin)
        plugin_before = plugin_namespace.get("config")
        if any(type(value) is not dict for value in (runtime_before, runtime_after, plugin_namespace)):
            return None
        if not isinstance(plugin_before, Mapping):
            return None
        if plugin_namespace.get("_is_disposed", False):
            return None
        descriptor = next(
            (base.__dict__["__dict__"] for base in binding.config_type.__mro__ if "__dict__" in base.__dict__),
            None,
        )
        if not isinstance(descriptor, GetSetDescriptorType):
            return None
        # Runtime-only plugin loading markers are not persisted model fields.
        for key, value in plugin_before.items():
            if key.startswith("$") and key not in expanded:
                expanded[key] = deepcopy(value)
        models = tuple(ConfiguredModel(model.name, model.alias) for model in fresh.models)
        return ModelReload(
            binding.config_key,
            binding,
            runtime_before,
            runtime_after,
            descriptor,
            plugin_namespace,
            plugin_before,
            expanded,
            models,
        )
    except ConfigValidationError:
        raise
    except Exception:
        raise ConfigValidationError("Model configuration could not be prepared", code="invalid_model_config") from None
