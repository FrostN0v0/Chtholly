"""Embedding-backed long-term profile and episodic memory store."""

from __future__ import annotations

from typing import Any
from datetime import datetime
from dataclasses import dataclass

import litellm
from sqlalchemy import delete
from arclet.entari.logger import log
from entari_plugin_database import select, get_session

from .eval import EvalResult
from ..config import LLMChatConfig
from ..models import UserMemory, UserProfileFact
from .profile import (
    ProfileFactSnapshot,
    decode_embedding,
    encode_embedding,
    cosine_similarity,
    merge_profile_snapshot,
)

_LOGGER = log.wrapper("[llm_chat]")


@dataclass(slots=True, frozen=True)
class MemoryContext:
    profile_facts: list[str]
    relevant_memories: list[str]


def _embedding_from_response(response: Any) -> list[float]:
    data = response["data"] if isinstance(response, dict) else response.data
    item = data[0]
    embedding = item.get("embedding") if isinstance(item, dict) else item.embedding
    return [float(value) for value in embedding]


async def embed_text(config: LLMChatConfig, text: str) -> list[float] | None:
    if not config.memory_enabled or not text.strip():
        return None
    try:
        response = await litellm.aembedding(
            model=config.memory_embedding_model,
            input=[text],
            api_key=config.memory_embedding_api_key,
            api_base=config.memory_embedding_base_url,
            encoding_format="float",
        )
        return _embedding_from_response(response)
    except Exception as exc:
        _LOGGER.warning(f"embedding failed: {exc!r}")
        return None


def _format_profile_fact(fact: UserProfileFact) -> str:
    return f"- {fact.category}:{fact.key}={fact.value}（置信{fact.confidence:.2f}，证据{fact.evidence_count}次）"


def _profile_sort_key(
    fact: UserProfileFact,
    query_embedding: list[float] | None,
) -> tuple[float, float, int]:
    embedding = decode_embedding(fact.embedding_json)
    if query_embedding is not None and embedding is not None:
        return (cosine_similarity(query_embedding, embedding), fact.confidence, fact.evidence_count)
    return (fact.confidence, float(fact.evidence_count), 0)


async def load_memory_context(config: LLMChatConfig, user_id: str, channel_id: str, query: str) -> MemoryContext:
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
        memories = (
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

    query_embedding = await embed_text(config, query)

    stable_facts = [fact for fact in profile_facts if fact.confidence >= config.profile_fact_min_confidence]
    stable_facts.sort(key=lambda fact: _profile_sort_key(fact, query_embedding), reverse=True)
    formatted_facts = [_format_profile_fact(fact) for fact in stable_facts[: config.memory_top_profile_facts]]

    relevant_memories: list[str] = []
    if query_embedding is not None:
        scored_memories: list[tuple[UserMemory, float]] = []
        for memory in memories:
            embedding = decode_embedding(memory.embedding_json)
            if embedding is None:
                continue
            score = cosine_similarity(query_embedding, embedding)
            if score >= config.memory_min_similarity:
                scored_memories.append((memory, score))
        scored_memories.sort(
            key=lambda item: (item[1], item[0].importance, item[0].created_at or datetime.min),
            reverse=True,
        )
        relevant_memories = [f"- {memory.text}" for memory, _ in scored_memories[: config.memory_top_memories]]

    return MemoryContext(profile_facts=formatted_facts, relevant_memories=relevant_memories)


async def _find_profile_fact(
    session: Any,
    user_id: str,
    channel_id: str,
    category: str,
    key: str,
) -> UserProfileFact | None:
    result = await session.execute(
        select(UserProfileFact)
        .where(
            UserProfileFact.user_id == user_id,
            UserProfileFact.channel_id == channel_id,
            UserProfileFact.category == category,
            UserProfileFact.key == key,
        )
        .order_by(UserProfileFact.id.asc())
    )
    return result.scalars().first()


async def apply_memory_updates(
    config: LLMChatConfig,
    user_id: str,
    channel_id: str,
    result: EvalResult,
) -> None:
    if not config.memory_enabled:
        return

    async with get_session() as session:
        for patch in result.profile_patches:
            fact = await _find_profile_fact(session, user_id, channel_id, patch.category, patch.key)
            existing = (
                None
                if fact is None
                else ProfileFactSnapshot(
                    value=fact.value,
                    confidence=fact.confidence,
                    evidence_count=fact.evidence_count,
                )
            )
            merged = merge_profile_snapshot(existing, patch)
            embedding = await embed_text(config, f"{patch.category}:{patch.key}:{merged.value}")
            embedding_json = encode_embedding(embedding) if embedding is not None else ""
            now = datetime.utcnow()

            if fact is None:
                session.add(
                    UserProfileFact(
                        user_id=user_id,
                        channel_id=channel_id,
                        category=patch.category,
                        key=patch.key,
                        value=merged.value,
                        confidence=merged.confidence,
                        evidence_count=merged.evidence_count,
                        last_evidence=patch.evidence,
                        embedding_json=embedding_json,
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                fact.value = merged.value
                fact.confidence = merged.confidence
                fact.evidence_count = merged.evidence_count
                fact.last_evidence = patch.evidence
                if embedding_json:
                    fact.embedding_json = embedding_json
                fact.updated_at = now

        for item in result.memory_items:
            embedding = await embed_text(config, item.text)
            session.add(
                UserMemory(
                    user_id=user_id,
                    channel_id=channel_id,
                    text=item.text,
                    importance=item.importance,
                    embedding_json=encode_embedding(embedding) if embedding is not None else "",
                    source="conversation",
                )
            )

        keep_count = max(0, config.memory_max_records_per_user)
        old_ids = (
            (
                await session.execute(
                    select(UserMemory.id)
                    .where(UserMemory.user_id == user_id, UserMemory.channel_id == channel_id)
                    .order_by(UserMemory.created_at.desc(), UserMemory.id.desc())
                    .offset(keep_count)
                )
            )
            .scalars()
            .all()
        )
        if old_ids:
            await session.execute(delete(UserMemory).where(UserMemory.id.in_(old_ids)))

        await session.commit()
