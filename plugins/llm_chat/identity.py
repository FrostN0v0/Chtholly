"""Unified Entari user identity boundary for llm_chat state."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable

from sqlalchemy import delete, update
from arclet.entari import At, Session
from entari_plugin_database import select, get_session
from sqlalchemy.ext.asyncio import AsyncSession

from .models import UserMemory, Conversation, UserRelation, UserProfileFact
from .perception import MentionedParticipant, get_channel_perception

MAX_MENTIONED_PARTICIPANTS = 10


@dataclass(frozen=True, slots=True)
class ChatIdentity:
    """Current platform identity resolved to one Entari user."""

    user_id: str
    display_name: str
    participant_ref: str


def _clean_text(value: object) -> str:
    return str(value).strip() if value is not None else ""


async def resolve_mentioned_participants(session: Session) -> list[MentionedParticipant]:
    """Resolve non-Bot At elements without exposing platform user IDs."""

    self_id = _clean_text(session.account.self_id)
    perception = get_channel_perception()
    resolved: list[MentionedParticipant] = []
    seen_user_ids: set[str] = set()
    for element in session.elements:
        if not isinstance(element, At):
            continue
        mention = element
        platform_user_id = _clean_text(mention.id)
        if not platform_user_id or platform_user_id == self_id or platform_user_id in seen_user_ids:
            continue
        if len(seen_user_ids) >= MAX_MENTIONED_PARTICIPANTS:
            break
        seen_user_ids.add(platform_user_id)
        display_name = _clean_text(mention.name)
        try:
            participant = await perception.resolve_participant_by_platform_user(session, platform_user_id)
        except Exception:
            participant = None
        if participant is not None:
            resolved.append(
                {
                    "display_name": display_name or participant.display_name,
                    "participant_ref": participant.public_ref,
                }
            )
        elif display_name:
            resolved.append({"display_name": display_name})
        else:
            continue

    return resolved


def _identity_sources(values: Iterable[str], target: str) -> set[str]:
    return {value.strip() for value in values if value and value.strip() and value.strip() != target}


async def _merge_relation(
    session: AsyncSession,
    channel_id: str,
    source_ids: set[str],
    target_id: str,
) -> None:
    rows = list(
        (
            await session.execute(
                select(UserRelation).where(
                    UserRelation.channel_id == channel_id,
                    UserRelation.user_id.in_([target_id, *source_ids]),
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return
    target = next((row for row in rows if row.user_id == target_id), None)
    latest = max(rows, key=lambda row: row.last_interaction)
    if target is None:
        target = latest
        target.user_id = target_id
    elif latest is not target:
        target.affection = latest.affection
        target.trust = latest.trust
        target.dependence = latest.dependence
        target.resentment = latest.resentment
        target.familiarity = latest.familiarity
        target.impression = latest.impression
        target.last_interaction = latest.last_interaction
    target.eval_counter = max(row.eval_counter for row in rows)
    stale_ids = [row.user_id for row in rows if row is not target]
    if stale_ids:
        await session.execute(
            delete(UserRelation).where(
                UserRelation.channel_id == channel_id,
                UserRelation.user_id.in_(stale_ids),
            )
        )


async def _merge_profile_facts(
    session: AsyncSession,
    channel_id: str,
    source_ids: set[str],
    target_id: str,
) -> None:
    rows = list(
        (
            await session.execute(
                select(UserProfileFact)
                .where(
                    UserProfileFact.channel_id == channel_id,
                    UserProfileFact.user_id.in_([target_id, *source_ids]),
                )
                .order_by(UserProfileFact.updated_at.asc(), UserProfileFact.id.asc())
            )
        )
        .scalars()
        .all()
    )
    keepers: dict[tuple[str, str], UserProfileFact] = {}
    stale_ids: list[int] = []
    for row in rows:
        key = (row.category, row.key)
        keeper = keepers.get(key)
        if keeper is None:
            row.user_id = target_id
            keepers[key] = row
            continue
        if row.updated_at >= keeper.updated_at:
            keeper.value = row.value
            keeper.confidence = row.confidence
            keeper.last_evidence = row.last_evidence
            keeper.embedding_json = row.embedding_json
            keeper.updated_at = row.updated_at
        keeper.evidence_count = max(keeper.evidence_count, row.evidence_count)
        keeper.created_at = min(keeper.created_at, row.created_at)
        stale_ids.append(row.id)
    if stale_ids:
        await session.execute(delete(UserProfileFact).where(UserProfileFact.id.in_(stale_ids)))


async def migrate_legacy_user_state(
    channel_id: str,
    source_user_ids: Iterable[str],
    target_user_id: str,
) -> None:
    """Move one channel's legacy or previously bound state to the current user ID."""

    source_ids = _identity_sources(source_user_ids, target_user_id)
    if not source_ids:
        return
    async with get_session() as raw_session:
        session = raw_session
        await _merge_relation(session, channel_id, source_ids, target_user_id)
        await _merge_profile_facts(session, channel_id, source_ids, target_user_id)
        await session.execute(
            update(UserMemory)
            .where(UserMemory.channel_id == channel_id, UserMemory.user_id.in_(source_ids))
            .values(user_id=target_user_id)
        )
        await session.execute(
            update(Conversation)
            .where(
                Conversation.channel_id == channel_id,
                Conversation.role == "user",
                Conversation.user_id.in_(source_ids),
            )
            .values(user_id=target_user_id)
        )
        await session.commit()


async def resolve_chat_identity(session: Session) -> ChatIdentity:
    """Resolve the current speaker and migrate channel state to its unified user ID."""

    platform_user = session.user
    participant = await get_channel_perception().resolve_current_participant(session)
    user_id = str(participant.person_id)
    platform_user_id = _clean_text(platform_user.id)
    platform_nickname = _clean_text(platform_user.name)
    group_card = _clean_text(session.member.nick if session.member else None)
    display_name = group_card or platform_nickname or "member"
    await migrate_legacy_user_state(
        session.channel.id,
        (
            platform_user_id,
            *(str(value) for value in participant.previous_person_ids),
        ),
        user_id,
    )
    return ChatIdentity(
        user_id=user_id,
        display_name=display_name,
        participant_ref=participant.public_ref,
    )
