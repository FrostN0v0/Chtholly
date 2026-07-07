"""Pure long-term profile and semantic memory helpers."""

from __future__ import annotations

import json
import math
from typing import Any
from dataclasses import dataclass

ALLOWED_PROFILE_CATEGORIES = frozenset(
    {
        "preference",
        "interest",
        "trait",
        "communication_style",
        "boundary",
        "relationship",
        "background",
    }
)
PROFILE_KEY_MAX_LEN = 24
PROFILE_VALUE_MAX_LEN = 80
PROFILE_EVIDENCE_MAX_LEN = 120
MEMORY_TEXT_MAX_LEN = 160
PROFILE_PATCH_LIMIT = 5
MEMORY_ITEM_LIMIT = 3
REINFORCE_BONUS = 0.12
CONFLICT_PENALTY = 0.15
REPLACE_MARGIN = 0.25


@dataclass(slots=True, frozen=True)
class ProfilePatch:
    category: str
    key: str
    value: str
    confidence: float
    evidence: str


@dataclass(slots=True, frozen=True)
class MemoryItem:
    text: str
    importance: float


@dataclass(slots=True, frozen=True)
class ProfileFactSnapshot:
    value: str
    confidence: float
    evidence_count: int


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def normalize_profile_patch(raw: object, *, min_confidence: float) -> ProfilePatch | None:
    if not isinstance(raw, dict):
        return None

    category = raw.get("category")
    key = raw.get("key")
    value = raw.get("value")
    confidence = raw.get("confidence")
    evidence = raw.get("evidence", "")

    if not isinstance(category, str) or category not in ALLOWED_PROFILE_CATEGORIES:
        return None
    if not isinstance(key, str) or not isinstance(value, str):
        return None
    if not _is_number(confidence):
        return None
    if not isinstance(evidence, str):
        evidence = ""

    key = key.strip()
    value = value.strip()
    evidence = evidence.strip()
    if not key or not value:
        return None

    normalized_confidence = _clamp01(float(confidence))
    if normalized_confidence < min_confidence:
        return None

    return ProfilePatch(
        category=category,
        key=key[:PROFILE_KEY_MAX_LEN],
        value=value[:PROFILE_VALUE_MAX_LEN],
        confidence=normalized_confidence,
        evidence=evidence[:PROFILE_EVIDENCE_MAX_LEN],
    )


def normalize_memory_item(raw: object) -> MemoryItem | None:
    if not isinstance(raw, dict):
        return None

    text = raw.get("text")
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text:
        return None

    importance = raw.get("importance", 0.5)
    if not _is_number(importance):
        importance = 0.5

    return MemoryItem(text=text[:MEMORY_TEXT_MAX_LEN], importance=_clamp01(float(importance)))


def merge_profile_snapshot(existing: ProfileFactSnapshot | None, patch: ProfilePatch) -> ProfileFactSnapshot:
    if existing is None:
        return ProfileFactSnapshot(value=patch.value, confidence=patch.confidence, evidence_count=1)

    evidence_count = existing.evidence_count + 1
    if existing.value == patch.value:
        confidence = min(1.0, max(existing.confidence, patch.confidence) + REINFORCE_BONUS)
        return ProfileFactSnapshot(value=existing.value, confidence=confidence, evidence_count=evidence_count)

    if patch.confidence >= existing.confidence + REPLACE_MARGIN:
        return ProfileFactSnapshot(value=patch.value, confidence=patch.confidence, evidence_count=evidence_count)

    confidence = max(0.0, existing.confidence - CONFLICT_PENALTY)
    return ProfileFactSnapshot(value=existing.value, confidence=confidence, evidence_count=evidence_count)


def decode_embedding(raw: str) -> list[float] | None:
    if not raw:
        return None
    try:
        values: Any = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(values, list) or not values:
        return None
    if not all(_is_number(value) for value in values):
        return None
    return [float(value) for value in values]


def encode_embedding(values: list[float]) -> str:
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"))


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    left_norm = math.sqrt(math.fsum(value * value for value in left))
    right_norm = math.sqrt(math.fsum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    dot = math.fsum(a * b for a, b in zip(left, right, strict=True))
    return dot / (left_norm * right_norm)


def rank_by_similarity(
    query_embedding: list[float],
    candidates: list[tuple[str, list[float]]],
    *,
    limit: int,
    min_similarity: float,
) -> list[tuple[str, float]]:
    scored = [
        (text, score)
        for text, embedding in candidates
        if (score := cosine_similarity(query_embedding, embedding)) >= min_similarity
    ]
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored[:limit]


def fact_rank_key(
    query_embedding: list[float] | None,
    embedding_json: str,
    confidence: float,
    evidence_count: int,
) -> tuple[float, float, int]:
    """Uniform profile-fact ranking key: similarity first, 0.0 when unavailable."""
    score = 0.0
    if query_embedding is not None:
        embedding = decode_embedding(embedding_json)
        if embedding is not None:
            score = cosine_similarity(query_embedding, embedding)
    return (score, confidence, evidence_count)
