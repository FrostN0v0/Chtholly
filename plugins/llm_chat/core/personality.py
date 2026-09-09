"""Immutable persona snapshots and framework-free configuration validation."""

from __future__ import annotations

import json
from typing import Protocol
from pathlib import PurePosixPath
from dataclasses import dataclass
import unicodedata
from collections.abc import Mapping


class PersonaDefinition(Protocol):
    name: str
    prompt: str
    reference_image: str | None
    appearance: str


class PersonaConfiguration(Protocol):
    @property
    def default_persona(self) -> str: ...

    @property
    def personas(self) -> Mapping[str, PersonaDefinition]: ...


def validate_persona_key(key: str) -> str:
    """Require a single exact command token; reject ambiguous Unicode spellings."""
    if not isinstance(key, str) or not key or unicodedata.normalize("NFKC", key) != key:
        raise ValueError("Persona keys must be nonempty normalized strings")
    if any(not (char.isalnum() or char in "_-") for char in key):
        raise ValueError("Persona keys may contain only letters, numbers, '_' and '-'")
    return key


def validate_persona_definition(persona: PersonaDefinition) -> None:
    if not isinstance(persona.name, str) or not persona.name.strip():
        raise ValueError("Persona name must not be empty")
    if any(unicodedata.category(char).startswith("C") for char in persona.name):
        raise ValueError("Persona name must not contain control characters")
    if not isinstance(persona.prompt, str) or not persona.prompt.strip():
        raise ValueError("Persona prompt must not be empty")
    if not isinstance(persona.appearance, str):
        raise ValueError("Persona appearance must be text")
    path = persona.reference_image
    if path is None:
        return
    if not isinstance(path, str) or not path or path != path.strip():
        raise ValueError("Persona reference image must be a nonempty relative path or null")
    normalized = path.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if (
        pure.is_absolute()
        or any(part in {"", ".", ".."} for part in normalized.split("/"))
        or ":" in normalized
        or any(unicodedata.category(char).startswith("C") for char in normalized)
    ):
        raise ValueError("Persona reference image must stay below resources/image")


def validate_personas(config: PersonaConfiguration) -> None:
    if not isinstance(config.personas, Mapping) or not config.personas:
        raise ValueError("At least one persona must be configured")
    seen: set[str] = set()
    for key, persona in config.personas.items():
        canonical = validate_persona_key(key).casefold()
        if canonical in seen:
            raise ValueError("Persona keys must be unique ignoring case")
        seen.add(canonical)
        validate_persona_definition(persona)
    validate_persona_key(config.default_persona)
    if config.default_persona not in config.personas:
        raise ValueError("default_persona must exactly match a configured persona key")


@dataclass(frozen=True, slots=True)
class ResolvedPersona:
    key: str
    name: str
    prompt: str
    reference_image: str | None
    appearance: str

    def snapshot(self) -> dict[str, str | None]:
        return {
            "key": self.key,
            "name": self.name,
            "prompt": self.prompt,
            "reference_image": self.reference_image,
            "appearance": self.appearance,
        }

    @property
    def baseline_text(self) -> str:
        """Fingerprint identity and content, including the paired visual reference."""
        return json.dumps(self.snapshot(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def resolve_persona(config: PersonaConfiguration, key: str | None = None) -> ResolvedPersona:
    validate_personas(config)
    selected = config.default_persona if key is None else validate_persona_key(key)
    if selected not in config.personas:
        raise ValueError("Unknown persona key")
    persona = config.personas[selected]
    return ResolvedPersona(
        key=selected,
        name=persona.name,
        prompt=persona.prompt,
        reference_image=persona.reference_image.replace("\\", "/") if persona.reference_image else None,
        appearance=persona.appearance,
    )
