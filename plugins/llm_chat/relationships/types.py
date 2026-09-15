"""Immutable ownership and version of one claimed relationship evaluation."""

from __future__ import annotations

from datetime import datetime
from dataclasses import dataclass

from ..core.types import JSONType


@dataclass(frozen=True, slots=True)
class EvaluationBatch:
    evaluation_ref: str
    user_id: str
    channel_id: str
    persona_prompt: str
    turn_ids: tuple[int, ...]
    event_ids: tuple[int, ...]
    episodes: tuple[dict[str, JSONType], ...]
    before: dict[str, JSONType]
    version: int
    queued_at: str
    started_at: datetime
    model: str


class RelationshipConflict(RuntimeError):
    """A newer owner or state version invalidated an in-flight evaluation."""
