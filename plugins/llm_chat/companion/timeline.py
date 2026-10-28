"""Read only confirmed autonomous conversation outputs, never reflection/audit data."""

from __future__ import annotations

from sqlalchemy import select
from entari_plugin_database import get_session

from ..models import AgentTurn, AgentEvent, ContextSession


async def confirmed_companion_outputs(
    context_session: ContextSession,
    *,
    limit: int = 12,
    session_factory=None,
) -> list[tuple[AgentTurn, list[AgentEvent]]]:
    """Expose visible receipts across the same scope/persona without changing history.

    Native purpose, trigger, event reference and receipt status remain intact.
    Only assistant_output events enter this projection; host contact goals,
    reflection results, tools and admin records are deliberately unreachable.
    """
    async with (session_factory or get_session)() as db:
        rows = (
            await db.execute(
                select(AgentTurn, AgentEvent)
                .join(ContextSession, ContextSession.id == AgentTurn.session_id)
                .join(AgentEvent, AgentEvent.turn_id == AgentTurn.id)
                .where(
                    ContextSession.scope_id == context_session.scope_id,
                    ContextSession.persona_hash == context_session.persona_hash,
                    ContextSession.status != "sealed",
                    ContextSession.purpose == "companion",
                    AgentTurn.trigger_kind == "autonomous",
                    AgentTurn.status.in_(("completed", "partial")),
                    AgentEvent.event_type == "assistant_output",
                    AgentEvent.model_visible.is_(True),
                    AgentEvent.status == "confirmed",
                )
                .order_by(AgentTurn.created_at.desc(), AgentTurn.id.desc(), AgentEvent.sequence.desc())
                .limit(max(1, min(32, limit)))
            )
        ).all()
    grouped: dict[int, tuple[AgentTurn, list[AgentEvent]]] = {}
    for turn, event in reversed(rows):
        grouped.setdefault(turn.id, (turn, []))[1].append(event)
    return list(grouped.values())
