"""Per-participant FIFO admission into cooperative ordinary-chat generations."""

from __future__ import annotations

from typing import Any
import asyncio
from functools import wraps
from contextlib import suppress
from collections import deque
from dataclasses import field, dataclass
from collections.abc import Callable, Coroutine

from arclet.entari import Session
from arclet.letoderea import BLOCK
from arclet.entari.logger import log
from arclet.letoderea.context import Contexts

from .steering import SteeringState, ParticipantScope, steering_scope, participant_scope
from .core.errors import summarize_exception

_LOGGER = log.wrapper("[llm_chat]")
_Handler = Callable[[Session, Contexts], Coroutine[Any, Any, object]]


@dataclass(slots=True)
class _ActiveParticipantTurn:
    task: asyncio.Task[object]
    state: SteeringState


@dataclass(slots=True, eq=False)
class _Admission:
    caller: asyncio.Task[Any] | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(slots=True)
class _ParticipantAdmissionQueue:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    waiting: deque[_Admission] = field(default_factory=deque)


_ACTIVE_PARTICIPANT_TURNS: dict[ParticipantScope, _ActiveParticipantTurn] = {}
_PARTICIPANT_ADMISSIONS: dict[ParticipantScope, _ParticipantAdmissionQueue] = {}
_UNLOAD_GENERATION = 0


def participant_turn_active(platform: str, account_id: str, channel_id: str, user_id: str) -> bool:
    active = _ACTIVE_PARTICIPANT_TURNS.get((platform, account_id, channel_id, user_id))
    return active is not None and not active.task.done()


async def _run_participant_turn(handler: _Handler, session: Session, ctx: Contexts, state: SteeringState) -> object:
    with steering_scope(state):
        try:
            return await handler(session, ctx)
        finally:
            try:
                await state.aclose()
            except Exception as exc:
                _LOGGER.warning(f"participant steering cleanup failed: {summarize_exception(exc)}")
            finally:
                active = _ACTIVE_PARTICIPANT_TURNS.get(state.scope)
                if active is not None and active.state is state:
                    _ACTIVE_PARTICIPANT_TURNS.pop(state.scope, None)


async def cancel_active_participant_turns() -> None:
    """Invalidate inherited scopes, then cancel and settle every unload-owned task."""
    global _UNLOAD_GENERATION
    _UNLOAD_GENERATION += 1
    active_turns = tuple(_ACTIVE_PARTICIPANT_TURNS.values())
    queued_callers: set[asyncio.Task[Any]] = set()
    for queue in tuple(_PARTICIPANT_ADMISSIONS.values()):
        for admission in queue.waiting:
            admission.ready.set()
            if admission.caller is not None and admission.caller is not asyncio.current_task():
                queued_callers.add(admission.caller)
    for active in active_turns:
        active.state.closed = True
        active.state.accepting = False
        if not active.task.done():
            active.task.cancel()
    for caller in queued_callers:
        caller.cancel()
    settling = {active.task for active in active_turns} | queued_callers
    if settling:
        await asyncio.gather(*settling, return_exceptions=True)


def _release_admission(queue: _ParticipantAdmissionQueue, admission: _Admission) -> None:
    with suppress(ValueError):
        queue.waiting.remove(admission)
    if queue.waiting:
        queue.waiting[0].ready.set()


def steer_participant_turn(handler: _Handler) -> _Handler:
    """Append addressed input without canceling the owner or another participant."""

    @wraps(handler)
    async def wrapped(session: Session, ctx: Contexts) -> object:
        scope = participant_scope(session)
        unload_generation = _UNLOAD_GENERATION
        queue = _PARTICIPANT_ADMISSIONS.setdefault(scope, _ParticipantAdmissionQueue())
        admission = _Admission(caller=asyncio.current_task())
        owner: asyncio.Task[object] | None = None
        async with queue.lock:
            queue.waiting.append(admission)
            if queue.waiting[0] is admission:
                admission.ready.set()
        try:
            while True:
                await admission.ready.wait()
                async with queue.lock:
                    if unload_generation != _UNLOAD_GENERATION:
                        raise asyncio.CancelledError("Participant steering plugin unloaded")
                    if queue.waiting[0] is not admission:
                        admission.ready.clear()
                        continue
                    active = _ACTIVE_PARTICIPANT_TURNS.get(scope)
                    if active is not None and active.task.done():
                        _ACTIVE_PARTICIPANT_TURNS.pop(scope, None)
                        active = None
                    if active is None:
                        state = SteeringState(session, ctx)
                        owner = asyncio.create_task(_run_participant_turn(handler, session, ctx, state))
                        _ACTIVE_PARTICIPANT_TURNS[scope] = _ActiveParticipantTurn(owner, state)
                        _release_admission(queue, admission)
                        received = False
                    else:
                        received = active.state.receive(session, ctx)
                        if received:
                            _release_admission(queue, admission)
                if owner is not None:
                    return await asyncio.shield(owner)
                assert active is not None
                if received:
                    await active.state.flush()
                    return BLOCK
                # The head retains its place while a sealed/full owner settles.
                # No scope admission lock or unrelated participant is held here.
                await asyncio.wait((active.task,))
        except asyncio.CancelledError:
            if owner is not None and not owner.done():
                owner.cancel()
                await asyncio.gather(owner, return_exceptions=True)
            raise
        finally:
            async with queue.lock:
                _release_admission(queue, admission)
                if not queue.waiting and scope not in _ACTIVE_PARTICIPANT_TURNS:
                    if _PARTICIPANT_ADMISSIONS.get(scope) is queue:
                        _PARTICIPANT_ADMISSIONS.pop(scope, None)

    return wrapped
