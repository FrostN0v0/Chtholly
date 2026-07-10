"""Long-term memory read and prompt formatting boundary."""

from __future__ import annotations

from datetime import datetime
from dataclasses import dataclass

from entari_plugin_database import select, get_session

from ..models import UserMemory, UserProfileFact
from .embedding import embed_text
from .config_types import LLMChatConfigLike
from ..core.profile import decode_embedding
from ..core.memory_policy import (
    MemoryCandidate,
    ProfileFactData,
    ProfileFactCandidate,
    group_profile_facts,
    normalize_prompt_text,
    select_relevant_memories,
    select_chat_profile_facts,
)


@dataclass(slots=True, frozen=True)
class MemoryContext:
    chat_profile: dict[str, list[str]]
    evaluator_profile_facts: list[ProfileFactData]
    relevant_memories: list[str]


async def load_memory_context(
    config: LLMChatConfigLike,
    user_id: str,
    channel_id: str,
    query: str,
) -> MemoryContext:
    """Load separate chat and evaluator views without mutating stored rows."""
    if not config.memory_enabled:
        return MemoryContext(
            chat_profile={},
            evaluator_profile_facts=[],
            relevant_memories=[],
        )

    async with get_session() as session:
        profile_rows = (
            (
                await session.execute(
                    select(UserProfileFact).where(
                        UserProfileFact.user_id == user_id,
                        UserProfileFact.channel_id == channel_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        memory_rows = (
            (
                await session.execute(
                    select(UserMemory).where(
                        UserMemory.user_id == user_id,
                        UserMemory.channel_id == channel_id,
                    )
                )
            )
            .scalars()
            .all()
        )

    query_embedding = await embed_text(config, query) if profile_rows or memory_rows else None

    profile_candidates = [
        ProfileFactCandidate(
            id=row.id,
            category=row.category,
            key=row.key,
            value=row.value,
            confidence=row.confidence,
            evidence_count=row.evidence_count,
            updated_at=row.updated_at,
            embedding=decode_embedding(row.embedding_json),
        )
        for row in profile_rows
    ]
    grouped_facts = group_profile_facts(
        profile_candidates,
        min_similarity=config.profile_alias_similarity,
    )

    evaluator_candidates = sorted(
        grouped_facts,
        key=lambda fact: (
            fact.confidence,
            fact.evidence_count,
            fact.updated_at or datetime.min,
            -fact.id,
        ),
        reverse=True,
    )[: max(0, config.memory_eval_profile_fact_limit)]
    evaluator_candidates.sort(key=lambda fact: (fact.category, fact.key))
    evaluator_profile_facts: list[ProfileFactData] = [
        ProfileFactData(
            category=fact.category,
            key=fact.key,
            value=fact.value,
            confidence=fact.confidence,
            aliases=list(fact.alias_keys),
        )
        for fact in evaluator_candidates
    ]

    chat_candidates = select_chat_profile_facts(
        [fact for fact in grouped_facts if fact.confidence >= config.profile_fact_min_confidence],
        query_embedding,
        limit=config.memory_top_profile_facts,
    )
    chat_profile: dict[str, list[str]] = {}
    for fact in chat_candidates:
        chat_profile.setdefault(fact.category, []).append(normalize_prompt_text(fact.value))

    memory_candidates = [
        MemoryCandidate(
            id=row.id,
            text=row.text,
            importance=row.importance,
            created_at=row.created_at,
            embedding=decode_embedding(row.embedding_json),
        )
        for row in memory_rows
    ]
    selected_memories = select_relevant_memories(
        query_embedding,
        memory_candidates,
        min_importance=config.memory_min_importance,
        min_similarity=config.memory_min_similarity,
        dedup_similarity=config.memory_prompt_dedup_similarity,
        limit=config.memory_top_memories,
    )

    return MemoryContext(
        chat_profile=chat_profile,
        evaluator_profile_facts=evaluator_profile_facts,
        relevant_memories=[normalize_prompt_text(memory.text) for memory in selected_memories],
    )
