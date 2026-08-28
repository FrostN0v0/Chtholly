"""Persistent chat scopes, context sessions, and turn lifecycle."""

from __future__ import annotations

from datetime import datetime, timedelta
from dataclasses import dataclass

from sqlalchemy import func, select
from arclet.entari import Session
from entari_plugin_database import get_session

from .models import AgentTurn, ChatScope, AgentEvent, ContextAnchor, ContextSession


@dataclass(frozen=True, slots=True)
class ScopeIdentity:
    platform: str
    account_id: str
    guild_id: str
    channel_id: str
    display_name: str


@dataclass(frozen=True, slots=True)
class BaselineFingerprint:
    model_name: str
    persona_hash: str
    system_version: str
    tool_schema_hash: str
    policy_version: str


@dataclass(frozen=True, slots=True)
class SessionHandle:
    scope: ChatScope
    session: ContextSession


def scope_identity(session: Session) -> ScopeIdentity:
    guild = getattr(session, "guild", None)
    channel = session.channel
    display_name = str(getattr(channel, "name", "") or getattr(guild, "name", "") or channel.id).strip()
    return ScopeIdentity(
        platform=str(session.account.platform),
        account_id=str(session.account.self_id),
        guild_id=str(getattr(guild, "id", "") or ""),
        channel_id=str(channel.id),
        display_name=display_name,
    )


def _fingerprint_matches(session: ContextSession, baseline: BaselineFingerprint) -> bool:
    return (
        session.model_name == baseline.model_name
        and session.persona_hash == baseline.persona_hash
        and session.system_version == baseline.system_version
        and session.tool_schema_hash == baseline.tool_schema_hash
        and session.policy_version == baseline.policy_version
    )


async def get_or_create_scope(identity: ScopeIdentity) -> ChatScope:
    async with get_session() as db:
        scope = (
            await db.execute(
                select(ChatScope).where(
                    ChatScope.platform == identity.platform,
                    ChatScope.account_id == identity.account_id,
                    ChatScope.channel_id == identity.channel_id,
                )
            )
        ).scalar_one_or_none()
        now = datetime.utcnow()
        if scope is None:
            scope = ChatScope(
                platform=identity.platform,
                account_id=identity.account_id,
                guild_id=identity.guild_id,
                channel_id=identity.channel_id,
                display_name=identity.display_name,
                created_at=now,
                updated_at=now,
            )
            db.add(scope)
            await db.flush()
        else:
            scope.guild_id = identity.guild_id
            scope.display_name = identity.display_name
            scope.updated_at = now
        await db.commit()
        await db.refresh(scope)
        return scope


async def get_scope_by_ref(scope_ref: str) -> ChatScope | None:
    async with get_session() as db:
        return (await db.execute(select(ChatScope).where(ChatScope.scope_ref == scope_ref))).scalar_one_or_none()


async def get_active_session(scope_id: int) -> ContextSession | None:
    async with get_session() as db:
        return (
            await db.execute(
                select(ContextSession)
                .where(ContextSession.scope_id == scope_id, ContextSession.status == "active")
                .order_by(ContextSession.sequence.desc())
                .limit(1)
            )
        ).scalar_one_or_none()


async def get_session_by_ref(session_ref: str) -> ContextSession | None:
    async with get_session() as db:
        return (
            await db.execute(select(ContextSession).where(ContextSession.session_ref == session_ref))
        ).scalar_one_or_none()


async def _next_session_sequence(db, scope_id: int) -> int:
    current = await db.scalar(select(func.max(ContextSession.sequence)).where(ContextSession.scope_id == scope_id))
    return int(current or 0) + 1


async def create_session(
    scope_id: int,
    baseline: BaselineFingerprint,
    *,
    start_reason: str,
    previous_session_id: int | None = None,
    handoff_json: str = "{}",
) -> ContextSession:
    async with get_session() as db:
        session = ContextSession(
            scope_id=scope_id,
            previous_session_id=previous_session_id,
            sequence=await _next_session_sequence(db, scope_id),
            status="active",
            start_reason=start_reason,
            model_name=baseline.model_name,
            persona_hash=baseline.persona_hash,
            system_version=baseline.system_version,
            tool_schema_hash=baseline.tool_schema_hash,
            policy_version=baseline.policy_version,
            handoff_json=handoff_json,
        )
        db.add(session)
        await db.commit()
        await db.refresh(session)
        return session


async def ensure_active_session(
    scope: ChatScope,
    baseline: BaselineFingerprint,
    *,
    idle_minutes: int,
    max_turns: int,
    now: datetime | None = None,
) -> tuple[ContextSession, str | None]:
    current = await get_active_session(scope.id)
    if current is None:
        return await create_session(scope.id, baseline, start_reason="initial"), None

    current_time = now or datetime.utcnow()
    reason: str | None = None
    if not _fingerprint_matches(current, baseline):
        reason = "runtime_change"
    elif max_turns > 0 and current.turn_count >= max_turns:
        reason = "turn_limit"
    elif (
        idle_minutes > 0
        and current.last_turn_at is not None
        and current_time - current.last_turn_at >= timedelta(minutes=idle_minutes)
    ):
        reason = "idle"
    return current, reason


async def close_session(session_id: int, reason: str, *, handoff_json: str = "{}") -> None:
    async with get_session() as db:
        current = await db.get(ContextSession, session_id)
        if current is None or current.status != "active":
            return
        current.status = "closed"
        current.close_reason = reason
        current.handoff_json = handoff_json
        current.closed_at = datetime.utcnow()
        await db.commit()


async def rollover_session(
    scope: ChatScope,
    current: ContextSession,
    baseline: BaselineFingerprint,
    *,
    reason: str,
    handoff_json: str = "{}",
    carry_handoff: bool = True,
) -> ContextSession:
    await close_session(current.id, reason, handoff_json=handoff_json)
    return await create_session(
        scope.id,
        baseline,
        start_reason=reason,
        previous_session_id=current.id,
        handoff_json=handoff_json if carry_handoff else "{}",
    )


async def start_turn(
    context_session: ContextSession,
    *,
    trigger_message_id: str,
    user_id: str,
    user_name: str,
    conversation_user_id: int | None,
    fresh_context: bool,
) -> AgentTurn:
    async with get_session() as db:
        sequence = (
            int(
                await db.scalar(select(func.max(AgentTurn.sequence)).where(AgentTurn.session_id == context_session.id))
                or 0
            )
            + 1
        )
        turn = AgentTurn(
            session_id=context_session.id,
            sequence=sequence,
            trigger_message_id=trigger_message_id,
            conversation_user_id=conversation_user_id,
            user_id=user_id,
            user_name=user_name,
            fresh_context=fresh_context,
        )
        db.add(turn)
        current = await db.get(ContextSession, context_session.id)
        if current is not None:
            current.last_turn_at = datetime.utcnow()
        await db.commit()
        await db.refresh(turn)
        return turn


async def list_scope_sessions(scope_id: int, *, limit: int = 20) -> list[ContextSession]:
    async with get_session() as db:
        return list(
            (
                await db.execute(
                    select(ContextSession)
                    .where(ContextSession.scope_id == scope_id)
                    .order_by(ContextSession.sequence.desc())
                    .limit(max(1, limit))
                )
            )
            .scalars()
            .all()
        )


async def list_scopes(*, limit: int = 100) -> list[ChatScope]:
    async with get_session() as db:
        return list(
            (await db.execute(select(ChatScope).order_by(ChatScope.updated_at.desc()).limit(max(1, limit))))
            .scalars()
            .all()
        )


async def load_scope_anchors(scope_id: int) -> list[tuple[ContextAnchor, AgentEvent]]:
    async with get_session() as db:
        rows = await db.execute(
            select(ContextAnchor, AgentEvent)
            .join(AgentEvent, AgentEvent.id == ContextAnchor.event_id)
            .where(ContextAnchor.scope_id == scope_id, ContextAnchor.active.is_(True))
            .order_by(ContextAnchor.created_at.asc())
        )
        return [(anchor, event) for anchor, event in rows.all()]


async def pin_event(
    scope_id: int,
    event_id: int,
    *,
    label: str,
    created_by_user_id: str,
) -> ContextAnchor:
    async with get_session() as db:
        existing = (
            await db.execute(
                select(ContextAnchor).where(
                    ContextAnchor.scope_id == scope_id,
                    ContextAnchor.event_id == event_id,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = ContextAnchor(
                scope_id=scope_id,
                event_id=event_id,
                label=label[:200],
                created_by_user_id=created_by_user_id,
                active=True,
            )
            db.add(existing)
        else:
            existing.label = label[:200]
            existing.created_by_user_id = created_by_user_id
            existing.active = True
        await db.commit()
        await db.refresh(existing)
        return existing


async def unpin_event(scope_id: int, event_id: int) -> bool:
    async with get_session() as db:
        anchor = (
            await db.execute(
                select(ContextAnchor).where(
                    ContextAnchor.scope_id == scope_id,
                    ContextAnchor.event_id == event_id,
                    ContextAnchor.active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if anchor is None:
            return False
        anchor.active = False
        await db.commit()
        return True


async def finish_turn(turn_id: int, *, status: str, final_text: str = "") -> None:
    async with get_session() as db:
        turn = await db.get(AgentTurn, turn_id)
        if turn is None:
            return
        was_running = turn.status == "running"
        turn.status = status
        turn.final_text = final_text
        turn.finished_at = datetime.utcnow()
        if was_running and status in {"completed", "partial"}:
            context_session = await db.get(ContextSession, turn.session_id)
            if context_session is not None:
                context_session.turn_count += 1
                context_session.last_turn_at = turn.finished_at
        await db.commit()


async def seal_scope_sessions(scope_id: int, *, reason: str = "hard_reset") -> None:
    async with get_session() as db:
        sessions = list(
            (await db.execute(select(ContextSession).where(ContextSession.scope_id == scope_id))).scalars().all()
        )
        closed_at = datetime.utcnow()
        for context_session in sessions:
            context_session.status = "sealed"
            context_session.close_reason = reason
            context_session.closed_at = context_session.closed_at or closed_at
        await db.commit()
