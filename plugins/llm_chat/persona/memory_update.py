"""Long-term memory write boundary after relationship evaluation."""

from __future__ import annotations

from typing import cast
from datetime import datetime
from dataclasses import field, dataclass

from sqlalchemy import case, func, update
from entari_plugin_database import select, get_session
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import UserMemory, UserProfileFact
from .embedding import embed_text
from ..core.eval import EvalResult
from .config_types import LLMChatConfigLike
from ..core.profile import (
    MEMORY_ITEM_LIMIT,
    ProfilePatch,
    ProfileFactSnapshot,
    decode_embedding,
    encode_embedding,
    cosine_similarity,
    match_duplicate_memory,
    merge_profile_snapshot,
)
from ..core.memory_policy import select_admitted_memory_indexes


@dataclass(slots=True, frozen=True)
class ExistingMemory:
    id: int
    text: str
    importance: float
    embedding: list[float] | None


@dataclass(slots=True)
class PendingMemory:
    text: str
    importance: float
    embedding_json: str
    embedding: list[float] | None


@dataclass(slots=True, frozen=True)
class MergedFact:
    patch: ProfilePatch
    snapshot: ProfileFactSnapshot
    embedding_json: str
    patch_vector: list[float] | None


@dataclass(slots=True, frozen=True)
class PreparedMemoryUpdates:
    user_id: str
    channel_id: str
    facts: tuple[MergedFact, ...] = ()
    importance_bumps: dict[int, float] = field(default_factory=dict)
    memories: tuple[PendingMemory, ...] = ()
    profile_value_similarity: float = 0.9
    memory_max_records: int = 200


def resolve_fact_embedding_update(
    merged_value: str,
    patch_value: str,
    patch_vector: list[float] | None,
    existing_vector: list[float] | None,
    backfill_vector: list[float] | None,
) -> tuple[str, bool]:
    """Decide whether a merged profile fact must rewrite its embedding."""
    if merged_value == patch_value:
        return (encode_embedding(patch_vector) if patch_vector is not None else "", True)
    if existing_vector is not None:
        return "", False
    if backfill_vector is not None:
        return encode_embedding(backfill_vector), True
    return "", False


async def _find_profile_fact(
    session: AsyncSession,
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


async def prepare_memory_updates(
    config: LLMChatConfigLike,
    user_id: str,
    channel_id: str,
    result: EvalResult,
) -> PreparedMemoryUpdates:
    """Perform memory reads and embedding calls before opening a write transaction."""
    if not config.memory_enabled:
        return PreparedMemoryUpdates(user_id, channel_id)

    patches: dict[tuple[str, str], ProfilePatch] = {}
    for patch in result.profile_patches:
        patches.setdefault((patch.category, patch.key), patch)
    eligible_memory_items = [item for item in result.memory_items if item.importance >= config.memory_min_importance][
        :MEMORY_ITEM_LIMIT
    ]
    if not patches and not eligible_memory_items:
        return PreparedMemoryUpdates(user_id, channel_id)

    snapshots: dict[tuple[str, str], ProfileFactSnapshot | None] = {}
    fact_vectors: dict[tuple[str, str], list[float] | None] = {}
    existing_memories: list[ExistingMemory] = []
    async with get_session() as raw_session:
        session = cast(AsyncSession, raw_session)
        for key, patch in patches.items():
            fact = await _find_profile_fact(session, user_id, channel_id, patch.category, patch.key)
            fact_vectors[key] = None if fact is None else decode_embedding(fact.embedding_json)
            snapshots[key] = (
                None
                if fact is None
                else ProfileFactSnapshot(
                    value=fact.value,
                    confidence=fact.confidence,
                    evidence_count=fact.evidence_count,
                )
            )
        if eligible_memory_items:
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
                ExistingMemory(row.id, row.text, row.importance, decode_embedding(row.embedding_json)) for row in rows
            ]

    merged_facts: list[MergedFact] = []
    for key, patch in patches.items():
        patch_vector = await embed_text(config, f"{patch.category}:{patch.key}:{patch.value}")
        existing_vector = fact_vectors[key]
        values_match: bool | None = None
        if patch_vector is not None and existing_vector is not None:
            score = cosine_similarity(patch_vector, existing_vector)
            values_match = score >= config.profile_value_similarity
        merged = merge_profile_snapshot(snapshots[key], patch, values_match=values_match)
        backfill_vector: list[float] | None = None
        if merged.value != patch.value and existing_vector is None:
            backfill_vector = await embed_text(config, f"{patch.category}:{patch.key}:{merged.value}")
        embedding_json, _ = resolve_fact_embedding_update(
            merged.value,
            patch.value,
            patch_vector,
            existing_vector,
            backfill_vector,
        )
        merged_facts.append(
            MergedFact(
                patch=patch,
                snapshot=merged,
                embedding_json=embedding_json,
                patch_vector=patch_vector,
            )
        )

    existing_candidates: list[tuple[int, str, list[float] | None]] = [
        (memory.id, memory.text, memory.embedding) for memory in existing_memories
    ]
    existing_importance = {memory.id: memory.importance for memory in existing_memories}
    importance_bumps: dict[int, float] = {}
    pending: list[PendingMemory] = []
    for item in eligible_memory_items:
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
        pending_candidates: list[tuple[int, str, list[float] | None]] = [
            (index, row.text, row.embedding) for index, row in enumerate(pending)
        ]
        pending_index = match_duplicate_memory(
            vector,
            item.text,
            pending_candidates,
            min_similarity=config.memory_dedup_similarity,
        )
        if pending_index is not None:
            pending[pending_index].importance = max(pending[pending_index].importance, item.importance)
            continue
        pending.append(
            PendingMemory(item.text, item.importance, encode_embedding(vector) if vector is not None else "", vector)
        )

    admitted_indexes = select_admitted_memory_indexes(
        [item.importance for item in pending],
        existing_count=len(existing_memories),
        limit=config.memory_max_records_per_user,
    )
    admitted_pending = [pending[index] for index in admitted_indexes]

    return PreparedMemoryUpdates(
        user_id,
        channel_id,
        tuple(merged_facts),
        importance_bumps,
        tuple(admitted_pending),
        config.profile_value_similarity,
        config.memory_max_records_per_user,
    )


async def apply_memory_updates(session: AsyncSession, prepared: PreparedMemoryUpdates) -> None:
    """Apply prepared memory writes in the caller's transaction without network IO or commit."""
    now = datetime.utcnow()
    user_id, channel_id = prepared.user_id, prepared.channel_id
    for fact in prepared.facts:
        patch = fact.patch
        current = await _find_profile_fact(session, user_id, channel_id, patch.category, patch.key)
        current_vector = decode_embedding(current.embedding_json) if current is not None else None
        values_match = (
            cosine_similarity(fact.patch_vector, current_vector) >= prepared.profile_value_similarity
            if fact.patch_vector is not None and current_vector is not None
            else None
        )
        current_snapshot = (
            ProfileFactSnapshot(current.value, current.confidence, current.evidence_count)
            if current is not None
            else None
        )
        merged = merge_profile_snapshot(current_snapshot, patch, values_match=values_match)
        backfill = decode_embedding(fact.embedding_json) if merged.value == fact.snapshot.value else None
        embedding_json, embedding_should_update = resolve_fact_embedding_update(
            merged.value,
            patch.value,
            fact.patch_vector,
            current_vector,
            backfill,
        )
        if current is None:
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
            current.value = merged.value
            current.confidence = merged.confidence
            current.evidence_count = merged.evidence_count
            current.last_evidence = patch.evidence
            current.updated_at = now
            if embedding_should_update:
                current.embedding_json = embedding_json
    for memory_id, importance in prepared.importance_bumps.items():
        await session.execute(
            update(UserMemory)
            .where(UserMemory.id == memory_id, UserMemory.user_id == user_id, UserMemory.channel_id == channel_id)
            .values(
                importance=case((UserMemory.importance < importance, importance), else_=UserMemory.importance),
                created_at=now,
            )
        )
    if prepared.memories:
        count = (
            await session.scalar(
                select(func.count())
                .select_from(UserMemory)
                .where(
                    UserMemory.user_id == user_id,
                    UserMemory.channel_id == channel_id,
                )
            )
            or 0
        )
        for item in prepared.memories[: max(0, prepared.memory_max_records - count)]:
            session.add(
                UserMemory(
                    user_id=user_id,
                    channel_id=channel_id,
                    text=item.text,
                    importance=item.importance,
                    embedding_json=item.embedding_json,
                    source="conversation",
                )
            )
