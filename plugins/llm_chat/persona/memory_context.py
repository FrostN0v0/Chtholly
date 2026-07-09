"""Long-term memory read and prompt formatting boundary."""

from __future__ import annotations

from datetime import datetime
from dataclasses import dataclass

from entari_plugin_database import select, get_session

from ..models import UserMemory, UserProfileFact
from .embedding import embed_text
from .config_types import LLMChatConfigLike
from ..core.profile import MemoryItem, fact_rank_key, decode_embedding, cosine_similarity


@dataclass(slots=True, frozen=True)
class ExistingMemory:
    id: int
    text: str
    importance: float
    embedding: list[float] | None
    created_at: datetime | None


@dataclass(slots=True, frozen=True)
class MemoryContext:
    profile_facts: list[str]
    relevant_memories: list[str]


def _format_profile_fact(fact: UserProfileFact) -> str:
    return f"- {fact.category}:{fact.key}={fact.value}（置信{fact.confidence:.2f}，证据{fact.evidence_count}次）"


def _format_memory(memory: MemoryItem) -> str:
    return f"- {memory.text}"


async def load_memory_context(config: LLMChatConfigLike, user_id: str, channel_id: str, query: str) -> MemoryContext:
    """Load stable profile facts and semantically relevant memories."""
    async with get_session() as session:
        profile_facts = (
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

    query_embedding: list[float] | None = None
    if profile_facts or memory_rows:
        query_embedding = await embed_text(config, query)

    stable_facts = [fact for fact in profile_facts if fact.confidence >= config.profile_fact_min_confidence]
    stable_facts.sort(
        key=lambda fact: fact_rank_key(query_embedding, fact.embedding_json, fact.confidence, fact.evidence_count),
        reverse=True,
    )
    formatted_facts = [_format_profile_fact(fact) for fact in stable_facts[: config.memory_top_profile_facts]]

    relevant_memories: list[MemoryItem] = []
    if query_embedding is not None:
        existing = [
            ExistingMemory(
                id=row.id,
                text=row.text,
                importance=row.importance,
                embedding=decode_embedding(row.embedding_json),
                created_at=row.created_at,
            )
            for row in memory_rows
        ]
        scored: list[tuple[ExistingMemory, float]] = []
        for memory in existing:
            if memory.embedding is None:
                continue
            score = cosine_similarity(query_embedding, memory.embedding)
            if score >= config.memory_min_similarity:
                scored.append((memory, score))
        scored.sort(key=lambda item: (item[1], item[0].importance, item[0].created_at or datetime.min), reverse=True)
        relevant_memories = [
            MemoryItem(text=memory.text, importance=memory.importance)
            for memory, _score in scored[: config.memory_top_memories]
        ]

    return MemoryContext(
        profile_facts=formatted_facts, relevant_memories=[_format_memory(item) for item in relevant_memories]
    )
