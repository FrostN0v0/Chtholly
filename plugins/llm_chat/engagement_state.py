"""Stateful engagement inputs and per-turn feedback persistence.

The pure policy lives in ``core.engagement``; this module owns the database and
session boundary so the policy itself stays deterministic and testable.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import case, update
from entari_plugin_database import select, get_session

from .models import BotState, Conversation, UserRelation
from .core.media import DECLINE_RECORD
from .core.engagement import TurnFeedback, EngagementSignals


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
    feedback: TurnFeedback,
) -> dict[str, float]:
    """Atomically apply one turn's drift to the latest stored state."""

    now = datetime.utcnow()
    resentment = UserRelation.resentment + feedback.irritation_delta
    familiarity = UserRelation.familiarity + feedback.closeness_delta
    mood = BotState.mood + feedback.mood_delta
    async with get_session() as session:
        result = await session.execute(
            update(UserRelation)
            .where(UserRelation.user_id == user_id, UserRelation.channel_id == channel_id)
            .values(
                resentment=case(
                    (resentment < 0.0, 0.0),
                    (resentment > 100.0, 100.0),
                    else_=resentment,
                ),
                familiarity=case(
                    (familiarity < 0.0, 0.0),
                    (familiarity > 100.0, 100.0),
                    else_=familiarity,
                ),
                last_interaction=now,
            )
        )
        if getattr(result, "rowcount", None) == 0:
            raise LookupError(f"relation not found: {user_id}/{channel_id}")

        state = await session.get(BotState, channel_id)
        if state is None:
            session.add(BotState(channel_id=channel_id, mood=max(-1.0, min(1.0, feedback.mood_delta))))
        else:
            await session.execute(
                update(BotState)
                .where(BotState.channel_id == channel_id)
                .values(
                    mood=case(
                        (mood < -1.0, -1.0),
                        (mood > 1.0, 1.0),
                        else_=mood,
                    ),
                    updated_at=now,
                )
            )
        await session.commit()

    async with get_session() as session:
        relation = await session.get(UserRelation, (user_id, channel_id))
        state = await session.get(BotState, channel_id)
        if relation is None or state is None:
            raise LookupError(f"feedback state not found: {user_id}/{channel_id}")
        return {
            "irritation": relation.resentment,
            "user_mood": state.mood,
            "familiarity": relation.familiarity,
        }


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
