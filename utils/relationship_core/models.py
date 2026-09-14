"""Pure relationship and affect value objects."""

from __future__ import annotations

from dataclasses import dataclass

AXIS_KEYS = ("affection", "trust", "dependence", "resentment", "familiarity")


@dataclass(frozen=True, slots=True)
class EmotionState:
    name: str
    intensity: float
    cause: str
    evidence_turn_ids: tuple[int, ...]
    updated_at: float


@dataclass(frozen=True, slots=True)
class RelationshipEvaluation:
    deltas: dict[str, float]
    description: str
    impression: str
    emotions: tuple[EmotionState, ...]
    processed_turn_ids: tuple[int, ...]
