"""Stateful engagement inputs and per-turn feedback persistence.

The pure policy lives in ``core.engagement``; this module owns the database and
session boundary so the policy itself stays deterministic and testable.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import update
from entari_plugin_database import select, get_session

from .models import BotState, Conversation, UserRelation
from .core.media import DECLINE_RECORD
from .core.engagement import TurnFeedback, EngagementSignals, apply_turn_feedback


async def collect_engagement_signals(
    *,
    user_id: str,
    channel_id: str,
    relation: UserRelation,
    user_mood: float,
    energy: float,
    text: str,
    is_command: bool = False,
    is_private: bool = False,
    is_operator: bool = False,
    requires_media_reply: bool = False,
    history_window: int = 12,
) -> EngagementSignals:
    """Build one decision input set from stored conversation and relation state."""

    async with get_session() as session:
        rows = list(
            (
                await session.execute(
                    select(Conversation)
                    .where(Conversation.channel_id == channel_id)
                    .order_by(Conversation.id.desc())
                    .limit(max(1, history_window))
                )
            )
            .scalars()
            .all()
        )

    consecutive = 0
    declines = 0
    seconds_since_last_reply = 3600.0
    now = datetime.utcnow()
    for row in rows:
        if row.role == "assistant":
            if row.content.strip() == DECLINE_RECORD:
                declines += 1
                continue
            seconds_since_last_reply = max(0.0, (now - row.created_at).total_seconds())
            break
        if row.user_id == user_id:
            consecutive += 1

    return EngagementSignals(
        affection=relation.affection,
        trust=relation.trust,
        familiarity=relation.familiarity,
        irritation=relation.resentment,
        user_mood=user_mood,
        energy=energy,
        text=text,
        consecutive_user_messages=max(1, consecutive),
        seconds_since_last_reply=seconds_since_last_reply,
        consecutive_declines=declines,
        is_command=is_command,
        is_private=is_private,
        is_operator=is_operator,
        requires_media_reply=requires_media_reply,
    )


async def persist_turn_feedback(
    *,
    user_id: str,
    channel_id: str,
    relation: UserRelation,
    user_mood: float,
    feedback: TurnFeedback,
) -> dict[str, float]:
    """Apply one turn's deterministic drift to stored relationship state."""

    updated = apply_turn_feedback(
        irritation=relation.resentment,
        user_mood=user_mood,
        familiarity=relation.familiarity,
        feedback=feedback,
    )
    async with get_session() as session:
        await session.execute(
            update(UserRelation)
            .where(UserRelation.user_id == user_id, UserRelation.channel_id == channel_id)
            .values(
                resentment=updated["irritation"],
                familiarity=updated["familiarity"],
                last_interaction=datetime.utcnow(),
            )
        )
        state = await session.get(BotState, channel_id)
        if state is None:
            session.add(BotState(channel_id=channel_id, mood=updated["user_mood"]))
        else:
            state.mood = updated["user_mood"]
            state.updated_at = datetime.utcnow()
        await session.commit()
    return updated


async def record_declined_turn(channel_id: str, user_id: str, user_name: str) -> None:
    """Record a deliberate silence so later turns can see the decline streak."""

    async with get_session() as session:
        session.add(
            Conversation(
                channel_id=channel_id,
                user_id=user_id,
                user_name=user_name,
                role="assistant",
                content=DECLINE_RECORD,
            )
        )
        await session.commit()
