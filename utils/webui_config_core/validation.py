"""Framework-free preflight checks shared with the privileged apply helper."""

from __future__ import annotations

from copy import deepcopy
import json
from collections.abc import Mapping

from .templates import TEMPLATE, environment_reference, expand_environment_template, restore_environment_templates

_SENSITIVE_FIELDS = frozenset(
    {
        "api_key",
        "token",
        "password",
        "access_token",
        "cookie",
        "cred",
        "role_token",
        "secret",
        "authorization",
        "client_secret",
        "refresh_token",
        "private_key",
    }
)
_OPENAI_BASE_URL = "https://api.openai.com/v1"


class ConfigValidationError(ValueError):
    """A value-free error suitable for an authenticated response or status file."""

    def __init__(self, message: str, *, code: str = "invalid_config", path: str = "config") -> None:
        self.code = code
        self.path = path
        super().__init__(f"{message} ({path})")


def _path(parent: str, key: object) -> str:
    # Keys are locations, not configuration values; hide unusual/free-text keys.
    text = str(key)
    safe = text if len(text) <= 80 and all(char.isalnum() or char in "_.$?:~-" for char in text) else "<field>"
    return f"{parent}.{safe}"


def _body(config: Mapping[str, object]) -> Mapping[str, object]:
    body = config.get("entari", config)
    if not isinstance(body, Mapping):
        raise ConfigValidationError("Entari configuration must be an object")
    return body


def _is_llm(key: object) -> bool:
    return isinstance(key, str) and key.lstrip("~?").split(".")[-1] in {"llm", "entari_plugin_llm"}


def normalize_model_defaults(config: Mapping[str, object]) -> dict[str, object]:
    """Remove scoped form placeholders, retaining global inheritance semantics."""
    result = deepcopy(dict(config))
    plugins = _body(result).get("plugins", {})
    if not isinstance(plugins, Mapping):
        return result
    for key, plugin in plugins.items():
        if not _is_llm(key) or not isinstance(plugin, Mapping):
            continue
        models = plugin.get("models")
        if not isinstance(models, list):
            continue
        for model in models:
            if not isinstance(model, dict):
                continue
            if model.get("api_key") in (None, ""):
                model.pop("api_key", None)
            if model.get("base_url") in (None, "", _OPENAI_BASE_URL):
                model.pop("base_url", None)
    return result


def _validate_models(plugin: Mapping[str, object], path: str, env: Mapping[str, str]) -> None:
    if "models" not in plugin:
        return
    models = plugin["models"]
    if not isinstance(models, list) or not models:
        raise ConfigValidationError("Models must be a nonempty array", path=f"{path}.models")
    identifiers: dict[str, int] = {}
    for index, model in enumerate(models):
        location = f"{path}.models[{index}]"
        if not isinstance(model, Mapping):
            raise ConfigValidationError("Each model must be an object", path=location)
        name = model.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ConfigValidationError("Model name must be a nonempty string", path=f"{location}.name")
        alias = model.get("alias")
        if alias is not None and not isinstance(alias, str):
            raise ConfigValidationError("Model alias must be a string or null", path=f"{location}.alias")
        for field, value in (("name", name), ("alias", alias)):
            if not value:
                continue
            value = expand_environment_template(value, env) or value
            if field == "name" and not value.strip():
                raise ConfigValidationError("Model name must be a nonempty string", path=f"{location}.name")
            if value in identifiers and identifiers[value] != index:
                raise ConfigValidationError(
                    "Model names and aliases must be unique", code="duplicate_model", path=f"{location}.{field}"
                )
            identifiers[value] = index
        for field in ("api_key", "base_url", "prompt"):
            if field in model and model[field] is not None and not isinstance(model[field], str):
                raise ConfigValidationError("Model field must be a string or null", path=f"{location}.{field}")
        if "extra" in model and not isinstance(model["extra"], Mapping):
            raise ConfigValidationError("Model extra must be an object", path=f"{location}.extra")


def _validate_values(value: object, env: Mapping[str, str], path: str, active: set[int]) -> None:
    container = isinstance(value, (Mapping, list))
    if container:
        if id(value) in active:
            raise ConfigValidationError("Configuration cannot contain recursive aliases", path=path)
        active.add(id(value))
    if isinstance(value, Mapping):
        for key, child in value.items():
            location = _path(path, key)
            if not isinstance(key, str):
                raise ConfigValidationError("Configuration keys must be strings", path=path)
            field = key.lower().replace("-", "_")
            if (field in _SENSITIVE_FIELDS or field.endswith("_api_key")) and child not in (None, ""):
                if not isinstance(child, str):
                    raise ConfigValidationError("Sensitive fields must be strings or null", path=location)
                matches = list(TEMPLATE.finditer(child))
                references = [environment_reference(match.group("expression")) for match in matches]
                if not references or any(reference is None for reference in references):
                    raise ConfigValidationError(
                        "Literal credentials cannot be saved; set an environment variable and use its reference",
                        code="literal_secret",
                        path=location,
                    )
                # A reference with a literal fallback is itself a credential store.
                if any(reference and (reference.default or reference.fallback) for reference in references):
                    raise ConfigValidationError(
                        "Sensitive environment references cannot contain literal defaults",
                        code="literal_secret",
                        path=location,
                    )
                residue = TEMPLATE.sub("", child).strip()
                if residue not in ("", "Bearer", "Basic"):
                    raise ConfigValidationError(
                        "Sensitive values must contain only environment references",
                        code="literal_secret",
                        path=location,
                    )
            _validate_values(child, env, location, active)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _validate_values(child, env, f"{path}[{index}]", active)
    elif isinstance(value, str):
        for match in TEMPLATE.finditer(value):
            expression = match.group("expression")
            reference = environment_reference(expression)
            if reference is not None and not env.get(reference.name) and not reference.optional:
                raise ConfigValidationError(
                    "Referenced environment variable is missing or empty", code="missing_environment", path=path
                )
            if reference is None and "env" in expression:
                raise ConfigValidationError("Unsupported environment expression", code="invalid_environment", path=path)
        if "${{" in TEMPLATE.sub("", value):
            raise ConfigValidationError("Malformed environment template", code="invalid_environment", path=path)
    if container:
        active.remove(id(value))


def validate_config(config: Mapping[str, object], env: Mapping[str, str]) -> Mapping[str, object]:
    if not isinstance(config, Mapping):
        raise ConfigValidationError("Configuration must be an object")
    body = _body(config)
    if "basic" in body and not isinstance(body["basic"], Mapping):
        raise ConfigValidationError("Basic configuration must be an object", path="config.basic")
    if "adapters" in body and (
        not isinstance(body["adapters"], list) or any(not isinstance(item, Mapping) for item in body["adapters"])
    ):
        raise ConfigValidationError("Adapters must be an array of objects", path="config.adapters")
    plugins = body.get("plugins", {})
    if not isinstance(plugins, Mapping):
        raise ConfigValidationError("Plugins must be an object", path="config.plugins")
    for key, plugin in plugins.items():
        location = _path("config.plugins", key)
        if isinstance(key, str) and key.startswith("$"):
            continue
        if not isinstance(plugin, Mapping):
            raise ConfigValidationError("Plugin configuration must be an object", path=location)
        if _is_llm(key):
            _validate_models(plugin, location, env)
    _validate_values(config, env, "config", set())
    return config


def prepare_config(
    candidate: Mapping[str, object], source: Mapping[str, object], env: Mapping[str, str]
) -> dict[str, object]:
    restored = restore_environment_templates(candidate, source, env)
    if not isinstance(restored, dict):
        raise ConfigValidationError("Configuration must be an object")
    normalized = normalize_model_defaults(restored)
    validate_config(normalized, env)
    return normalized


def same_config(left: object, right: object) -> bool:
    """Compare values without equating boolean and numeric configuration."""
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        return left.keys() == right.keys() and all(same_config(value, right[key]) for key, value in left.items())
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(same_config(a, b) for a, b in zip(left, right))
    return type(left) is type(right) and left == right


def validate_candidate(data: bytes, env: Mapping[str, str]) -> Mapping[str, object]:
    """Decode YAML/JSON bytes and validate without Entari or plugin imports."""
    try:
        text = data.decode("utf-8-sig")
        try:
            config = json.loads(text)
        except json.JSONDecodeError:
            from ruamel.yaml import YAML

            config = YAML(typ="safe").load(text)
    except Exception:
        # Parser errors routinely contain a source line, possibly a credential.
        raise ConfigValidationError("Configuration is not valid UTF-8 YAML or JSON", code="invalid_document") from None
    return validate_config(config, env)
