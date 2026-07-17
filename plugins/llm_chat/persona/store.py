"""DB load/save helpers for relations, bot mood and decay."""

from datetime import datetime, timedelta

from sqlalchemy import delete, update
from entari_plugin_database import select, get_session

from ..models import BotState, Conversation, UserRelation, UserProfileFact

AFFECTION_BASELINE = 30.0
TRUST_BASELINE = 30.0
DAILY_DRIFT = 1.0
MINOR_DRIFT = 0.5
FAMILIARITY_DECAY = 0.5
FACT_IDLE_DAYS = 30
FACT_DAILY_DECAY = 0.01
FACT_CULL_THRESHOLD = 0.2


async def get_relation(user_id: str, channel_id: str) -> UserRelation:
    async with get_session() as session:
        rel = await session.get(UserRelation, (user_id, channel_id))
        if rel is None:
            rel = UserRelation(user_id=user_id, channel_id=channel_id)
            session.add(rel)
            await session.commit()
            await session.refresh(rel)
        return rel


async def save_relation(
    user_id: str,
    channel_id: str,
    *,
    axes: dict[str, float],
    impression: str,
    familiarity: float,
    eval_counter: int,
) -> None:
    async with get_session() as session:
        await session.execute(
            update(UserRelation)
            .where(UserRelation.user_id == user_id, UserRelation.channel_id == channel_id)
            .values(
                affection=axes["affection"],
                trust=axes["trust"],
                dependence=axes["dependence"],
                resentment=axes["resentment"],
                familiarity=familiarity,
                impression=impression,
                eval_counter=eval_counter,
                last_interaction=datetime.utcnow(),
            )
        )
        await session.commit()


async def get_mood(channel_id: str) -> float:
    async with get_session() as session:
        state = await session.get(BotState, channel_id)
        return state.mood if state else 0.0


async def set_mood(channel_id: str, mood: float) -> None:
    mood = max(-1.0, min(1.0, mood))
    async with get_session() as session:
        state = await session.get(BotState, channel_id)
        if state is None:
            session.add(BotState(channel_id=channel_id, mood=mood))
        else:
            state.mood = mood
            state.updated_at = datetime.utcnow()
        await session.commit()


async def load_history(channel_id: str, limit: int) -> list[Conversation]:
    async with get_session() as session:
        rows = (
            (
                await session.execute(
                    select(Conversation)
                    .where(Conversation.channel_id == channel_id)
                    .order_by(Conversation.id.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return list(reversed(rows))


async def append_message(channel_id: str, user_id: str, user_name: str, role: str, content: str) -> int:
    async with get_session() as session:
        message = Conversation(
            channel_id=channel_id,
            user_id=user_id,
            user_name=user_name,
            role=role,
            content=content,
        )
        session.add(message)
        await session.flush()
        message_id = message.id
        await session.commit()
        return message_id


async def delete_message(message_id: int | None) -> None:
    if message_id is None:
        return
    async with get_session() as session:
        await session.execute(delete(Conversation).where(Conversation.id == message_id))
        await session.commit()


def _drift(value: float, baseline: float, step: float) -> float:
    if value > baseline:
        return max(baseline, value - step)
    if value < baseline:
        return min(baseline, value + step)
    return value


async def nightly_decay() -> None:
    """Time physics only: mood halves, axes drift, idle profile facts fade."""
    async with get_session() as session:
        states = (await session.execute(select(BotState))).scalars().all()
        for state in states:
            state.mood *= 0.5
        relations = (await session.execute(select(UserRelation))).scalars().all()
        for rel in relations:
            rel.affection = _drift(rel.affection, AFFECTION_BASELINE, DAILY_DRIFT)
            rel.trust = _drift(rel.trust, TRUST_BASELINE, DAILY_DRIFT)
            rel.dependence = _drift(rel.dependence, 0.0, MINOR_DRIFT)
            rel.resentment = _drift(rel.resentment, 0.0, MINOR_DRIFT)
            rel.familiarity = max(0.0, rel.familiarity - FAMILIARITY_DECAY)

        # Idle profile facts fade nightly and are culled once too weak to
        # matter; re-mention revives them through the merge reinforce path.
        idle_cutoff = datetime.utcnow() - timedelta(days=FACT_IDLE_DAYS)
        await session.execute(
            update(UserProfileFact)
            .where(UserProfileFact.updated_at < idle_cutoff)
            .values(confidence=UserProfileFact.confidence - FACT_DAILY_DECAY)
        )
        await session.execute(
            delete(UserProfileFact).where(
                UserProfileFact.updated_at < idle_cutoff,
                UserProfileFact.confidence < FACT_CULL_THRESHOLD,
            )
        )
        await session.commit()
