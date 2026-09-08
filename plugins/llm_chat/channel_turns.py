"""Per-participant latest-turn cancellation within public channels."""

from __future__ import annotations

from typing import Any
import asyncio
from functools import wraps
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass
from collections.abc import Callable, Coroutine

from arclet.entari import Session
from arclet.letoderea import BLOCK
from arclet.entari.logger import log
from arclet.letoderea.context import Contexts

from .core.errors import summarize_exception

_LOGGER = log.wrapper("[llm_chat]")
_ParticipantTurnScope = tuple[str, str, str, str]


@dataclass(slots=True)
class _ParticipantTurnState:
    superseded: bool = False


@dataclass(slots=True)
class _ActiveParticipantTurn:
    task: asyncio.Task[object]
    generation: int
    state: _ParticipantTurnState


_ACTIVE_PARTICIPANT_TURNS: dict[_ParticipantTurnScope, _ActiveParticipantTurn] = {}
_PARTICIPANT_TURN_GENERATIONS: dict[_ParticipantTurnScope, int] = {}
_CURRENT_PARTICIPANT_TURN_STATE: ContextVar[_ParticipantTurnState | None] = ContextVar(
    "llm_chat_participant_turn_state",
    default=None,
)


def _participant_turn_scope(session: Session) -> _ParticipantTurnScope:
    return (
        str(session.account.platform),
        str(session.account.self_id),
        str(session.channel.id),
        str(session.user.id),
    )


def current_participant_turn_superseded() -> bool:
    state = _CURRENT_PARTICIPANT_TURN_STATE.get()
    return state.superseded if state is not None else False


async def _run_participant_turn(
    handler: Callable[[Session, Contexts], Coroutine[Any, Any, object]],
    session: Session,
    ctx: Contexts,
    state: _ParticipantTurnState,
) -> object:
    token = _CURRENT_PARTICIPANT_TURN_STATE.set(state)
    try:
        return await handler(session, ctx)
    finally:
        _CURRENT_PARTICIPANT_TURN_STATE.reset(token)


def cancel_active_participant_turns() -> None:
    for active in tuple(_ACTIVE_PARTICIPANT_TURNS.values()):
        if not active.task.done():
            active.task.cancel()
    _ACTIVE_PARTICIPANT_TURNS.clear()
    _PARTICIPANT_TURN_GENERATIONS.clear()


def latest_participant_turn(
    handler: Callable[[Session, Contexts], Coroutine[Any, Any, object]],
) -> Callable[[Session, Contexts], Coroutine[Any, Any, object]]:
    """Cancel only an older turn from the same participant and channel."""

    @wraps(handler)
    async def wrapped(session: Session, ctx: Contexts) -> object:
        scope = _participant_turn_scope(session)
        generation = _PARTICIPANT_TURN_GENERATIONS.get(scope, 0) + 1
        _PARTICIPANT_TURN_GENERATIONS[scope] = generation

        previous = _ACTIVE_PARTICIPANT_TURNS.get(scope)
        if previous is not None and not previous.task.done():
            previous.state.superseded = True
            previous.task.cancel()
            try:
                await previous.task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                _LOGGER.warning(f"superseded participant turn cleanup failed: {summarize_exception(exc)}")

        if _PARTICIPANT_TURN_GENERATIONS.get(scope) != generation:
            return BLOCK

        state = _ParticipantTurnState()
        task = asyncio.create_task(_run_participant_turn(handler, session, ctx, state))
        active = _ActiveParticipantTurn(task=task, generation=generation, state=state)
        _ACTIVE_PARTICIPANT_TURNS[scope] = active
        try:
            return await task
        except asyncio.CancelledError:
            if active.state.superseded:
                return BLOCK
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            raise
        finally:
            if _ACTIVE_PARTICIPANT_TURNS.get(scope) is active:
                _ACTIVE_PARTICIPANT_TURNS.pop(scope, None)
            if _PARTICIPANT_TURN_GENERATIONS.get(scope) == generation:
                _PARTICIPANT_TURN_GENERATIONS.pop(scope, None)

    return wrapped
