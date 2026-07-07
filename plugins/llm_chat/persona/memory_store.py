"""Embedding-backed long-term profile and episodic memory store."""

from __future__ import annotations

from typing import Any
from datetime import datetime
from dataclasses import dataclass

import litellm
from sqlalchemy import delete, update
from arclet.entari.logger import log
from entari_plugin_database import select, get_session

from .eval import EvalResult
from ..config import LLMChatConfig
from ..models import UserMemory, UserProfileFact
from .profile import (
    ProfilePatch,
    ProfileFactSnapshot,
    fact_rank_key,
    decode_embedding,
    encode_embedding,
    cosine_similarity,
    match_duplicate_memory,
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

    # Skip the query-embedding call entirely when there is nothing to rank.
    query_embedding: list[float] | None = None
    if profile_facts or memories:
        query_embedding = await embed_text(config, query)

    stable_facts = [fact for fact in profile_facts if fact.confidence >= config.profile_fact_min_confidence]
    stable_facts.sort(
        key=lambda fact: fact_rank_key(query_embedding, fact.embedding_json, fact.confidence, fact.evidence_count),
        reverse=True,
    )
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

    # Deduplicate patches by (category, key), keeping the first occurrence.
    patches: dict[tuple[str, str], ProfilePatch] = {}
    for patch in result.profile_patches:
        patches.setdefault((patch.category, patch.key), patch)
    if not patches and not result.memory_items:
        return

    # Read phase: snapshot merge targets and dedup candidates in a short-lived session.
    fact_ids: dict[tuple[str, str], int | None] = {}
    snapshots: dict[tuple[str, str], ProfileFactSnapshot | None] = {}
    existing_memories: list[tuple[int, str, float, list[float] | None]] = []
    async with get_session() as session:
        for key, patch in patches.items():
            fact = await _find_profile_fact(session, user_id, channel_id, patch.category, patch.key)
            fact_ids[key] = None if fact is None else fact.id
            snapshots[key] = (
                None
                if fact is None
                else ProfileFactSnapshot(
                    value=fact.value,
                    confidence=fact.confidence,
                    evidence_count=fact.evidence_count,
                )
            )
        if result.memory_items:
            rows = (
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
            existing_memories = [
                (row.id, row.text, row.importance, decode_embedding(row.embedding_json)) for row in rows
            ]

    # Embed phase: network I/O runs with no DB session or transaction open.
    merged_facts: list[tuple[ProfilePatch, ProfileFactSnapshot, str]] = []
    for key, patch in patches.items():
        merged = merge_profile_snapshot(snapshots[key], patch)
        embedding = await embed_text(config, f"{patch.category}:{patch.key}:{merged.value}")
        merged_facts.append((patch, merged, encode_embedding(embedding) if embedding is not None else ""))

    existing_candidates = [(mem_id, text, vector) for mem_id, text, _importance, vector in existing_memories]
    existing_importance = {mem_id: importance for mem_id, _text, importance, _vector in existing_memories}
    importance_bumps: dict[int, float] = {}
    # Pending rows: [text, importance, embedding_json, vector].
    pending: list[list[Any]] = []
    for item in result.memory_items:
        vector = await embed_text(config, item.text)
        duplicate_id = match_duplicate_memory(
            vector,
            item.text,
            existing_candidates,
            min_similarity=config.memory_dedup_similarity,
        )
        if duplicate_id is not None:
            current = importance_bumps.get(duplicate_id, existing_importance[duplicate_id])
            importance_bumps[duplicate_id] = max(current, item.importance)
            continue
        pending_candidates = [(index, row[0], row[3]) for index, row in enumerate(pending)]
        pending_index = match_duplicate_memory(
            vector,
            item.text,
            pending_candidates,
            min_similarity=config.memory_dedup_similarity,
        )
        if pending_index is not None:
            pending[pending_index][1] = max(pending[pending_index][1], item.importance)
            continue
        pending.append([item.text, item.importance, encode_embedding(vector) if vector is not None else "", vector])

    # Write phase: one short transaction, no network awaits inside.
    now = datetime.utcnow()
    async with get_session() as session:
        for patch, merged, embedding_json in merged_facts:
            fact_id = fact_ids[(patch.category, patch.key)]
            if fact_id is None:
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
                values: dict[str, Any] = {
                    "value": merged.value,
                    "confidence": merged.confidence,
                    "evidence_count": merged.evidence_count,
                    "last_evidence": patch.evidence,
                    "updated_at": now,
                }
                if embedding_json:
                    values["embedding_json"] = embedding_json
                await session.execute(update(UserProfileFact).where(UserProfileFact.id == fact_id).values(**values))

        # Reinforced memories refresh created_at so pruning treats them as fresh.
        for mem_id, importance in importance_bumps.items():
            await session.execute(
                update(UserMemory).where(UserMemory.id == mem_id).values(importance=importance, created_at=now)
            )

        for text, importance, embedding_json, _vector in pending:
            session.add(
                UserMemory(
                    user_id=user_id,
                    channel_id=channel_id,
                    text=text,
                    importance=importance,
                    embedding_json=embedding_json,
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
