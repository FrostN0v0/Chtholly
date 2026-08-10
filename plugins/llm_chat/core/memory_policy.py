"""Pure read-time profile and memory selection policies."""

from __future__ import annotations

from typing import TypedDict
from datetime import datetime
from dataclasses import replace, dataclass
from collections.abc import Sequence

from .profile import cosine_similarity


class ProfileFactData(TypedDict):
    category: str
    key: str
    value: str
    confidence: float
    aliases: list[str]


@dataclass(slots=True, frozen=True)
class ProfileFactCandidate:
    id: int
    category: str
    key: str
    value: str
    confidence: float
    evidence_count: int
    updated_at: datetime | None
    embedding: list[float] | None
    alias_keys: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class MemoryCandidate:
    id: int
    text: str
    importance: float
    created_at: datetime | None
    embedding: list[float] | None


def normalize_prompt_text(text: str) -> str:
    return " ".join(text.split())


def _profile_candidate_rank(
    fact: ProfileFactCandidate,
) -> tuple[float, int, datetime, int]:
    return (
        fact.confidence,
        fact.evidence_count,
        fact.updated_at or datetime.min,
        -fact.id,
    )


def _embedding_similarity(
    left: list[float] | None,
    right: list[float] | None,
) -> float | None:
    if left is None or right is None or not left or len(left) != len(right):
        return None
    return cosine_similarity(left, right)


def _profile_identity(fact: ProfileFactCandidate) -> tuple[str, str, str]:
    return (
        fact.category,
        normalize_prompt_text(fact.key),
        normalize_prompt_text(fact.value),
    )


def group_profile_facts(
    facts: Sequence[ProfileFactCandidate],
    *,
    min_similarity: float,
) -> list[ProfileFactCandidate]:
    """Build non-transitive, representative-greedy profile groups."""
    ordered = sorted(facts, key=_profile_candidate_rank, reverse=True)
    groups: list[tuple[ProfileFactCandidate, list[ProfileFactCandidate]]] = []

    for candidate in ordered:
        candidate_identity = _profile_identity(candidate)
        exact_group_index: int | None = None
        for index, (representative, _) in enumerate(groups):
            if candidate_identity == _profile_identity(representative):
                exact_group_index = index
                break

        if exact_group_index is not None:
            groups[exact_group_index][1].append(candidate)
            continue

        best_group_index: int | None = None
        best_similarity: float | None = None
        for index, (representative, _) in enumerate(groups):
            if candidate.category != representative.category:
                continue
            similarity = _embedding_similarity(
                candidate.embedding,
                representative.embedding,
            )
            if similarity is None or similarity < min_similarity:
                continue
            if best_similarity is None or similarity > best_similarity:
                best_group_index = index
                best_similarity = similarity

        if best_group_index is None:
            groups.append((candidate, [candidate]))
        else:
            groups[best_group_index][1].append(candidate)

    grouped: list[ProfileFactCandidate] = []
    for representative, members in groups:
        aliases = tuple(sorted({member.key for member in members if member.key != representative.key}))
        grouped.append(replace(representative, alias_keys=aliases))
    grouped.sort(key=lambda fact: fact.id)
    return grouped


def profile_retrieval_score(
    query_embedding: list[float] | None,
    fact: ProfileFactCandidate,
) -> float:
    similarity = 0.0
    if query_embedding is not None and fact.embedding is not None:
        similarity = cosine_similarity(query_embedding, fact.embedding)
    return 0.70 * similarity + 0.20 * fact.confidence + 0.10 * min(fact.evidence_count, 5) / 5


def _chat_fact_rank(
    fact: ProfileFactCandidate,
    query_embedding: list[float] | None,
) -> tuple[float, float, int, int]:
    return (
        profile_retrieval_score(query_embedding, fact),
        fact.confidence,
        fact.evidence_count,
        -fact.id,
    )


def _rank_chat_facts(
    facts: Sequence[ProfileFactCandidate],
    query_embedding: list[float] | None,
) -> list[ProfileFactCandidate]:
    return sorted(
        facts,
        key=lambda fact: _chat_fact_rank(fact, query_embedding),
        reverse=True,
    )


def select_chat_profile_facts(
    facts: Sequence[ProfileFactCandidate],
    query_embedding: list[float] | None,
    *,
    limit: int,
) -> list[ProfileFactCandidate]:
    if limit <= 0:
        return []

    interaction = _rank_chat_facts(
        [fact for fact in facts if fact.category in {"boundary", "communication_style"}],
        query_embedding,
    )
    boundaries = [fact for fact in interaction if fact.category == "boundary"]
    if boundaries:
        selected_boundary = boundaries[0]
        interaction_selection = [selected_boundary]
        interaction_selection.extend(fact for fact in interaction if fact is not selected_boundary)
        interaction_selection = interaction_selection[:2]
    else:
        interaction_selection = interaction[:2]

    preferences = _rank_chat_facts(
        [fact for fact in facts if fact.category in {"preference", "interest"}],
        query_embedding,
    )[:2]
    context = _rank_chat_facts(
        [
            fact
            for fact in facts
            if fact.category == "background"
            or (fact.category == "trait" and fact.confidence >= 0.80 and fact.evidence_count >= 2)
        ],
        query_embedding,
    )[:1]
    relationship = _rank_chat_facts(
        [fact for fact in facts if fact.category == "relationship"],
        query_embedding,
    )[:1]

    return (interaction_selection + preferences + context + relationship)[:limit]


def select_relevant_memories(
    query_embedding: list[float] | None,
    memories: Sequence[MemoryCandidate],
    *,
    min_importance: float,
    min_similarity: float,
    dedup_similarity: float,
    limit: int,
) -> list[MemoryCandidate]:
    if query_embedding is None or not query_embedding or limit <= 0:
        return []

    ranked: list[tuple[MemoryCandidate, float]] = []
    for memory in memories:
        if memory.importance < min_importance:
            continue
        similarity = _embedding_similarity(query_embedding, memory.embedding)
        if similarity is None or similarity < min_similarity:
            continue
        ranked.append((memory, 0.80 * similarity + 0.20 * memory.importance))

    ranked.sort(
        key=lambda item: (
            item[1],
            item[0].created_at or datetime.min,
            item[0].id,
        ),
        reverse=True,
    )

    selected: list[MemoryCandidate] = []
    selected_texts: set[str] = set()
    for memory, _ in ranked:
        normalized_text = normalize_prompt_text(memory.text)
        if normalized_text in selected_texts:
            continue
        if any(
            (similarity := _embedding_similarity(memory.embedding, existing.embedding)) is not None
            and similarity >= dedup_similarity
            for existing in selected
        ):
            continue
        selected.append(memory)
        selected_texts.add(normalized_text)
        if len(selected) >= limit:
            break
    return selected


def select_admitted_memory_indexes(
    importances: Sequence[float],
    *,
    existing_count: int,
    limit: int,
) -> list[int]:
    available = max(0, limit - existing_count)
    if available == 0:
        return []
    ranked_indexes = sorted(
        range(len(importances)),
        key=lambda index: (-importances[index], index),
    )
    return ranked_indexes[:available]
