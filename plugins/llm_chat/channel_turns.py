"""Per-channel latest-turn cancellation for claimed llm_chat generations."""

from __future__ import annotations

from typing import Any
import asyncio
from functools import wraps
from contextlib import suppress
from dataclasses import dataclass
from collections.abc import Callable, Coroutine

from arclet.entari import Session
from arclet.letoderea import BLOCK
from arclet.entari.logger import log
from arclet.letoderea.context import Contexts

from .core.errors import summarize_exception

_LOGGER = log.wrapper("[llm_chat]")


@dataclass(slots=True)
class _ActiveChannelTurn:
    task: asyncio.Task[object]
    generation: int
    superseded: bool = False


_ACTIVE_CHANNEL_TURNS: dict[str, _ActiveChannelTurn] = {}
_CHANNEL_TURN_GENERATIONS: dict[str, int] = {}


def cancel_active_channel_turns() -> None:
    for active in tuple(_ACTIVE_CHANNEL_TURNS.values()):
        if not active.task.done():
            active.task.cancel()
    _ACTIVE_CHANNEL_TURNS.clear()
    _CHANNEL_TURN_GENERATIONS.clear()


def latest_channel_turn(
    handler: Callable[[Session, Contexts], Coroutine[Any, Any, object]],
) -> Callable[[Session, Contexts], Coroutine[Any, Any, object]]:
    """Cancel an older generation before starting the newest channel turn."""

    @wraps(handler)
    async def wrapped(session: Session, ctx: Contexts) -> object:
        channel_id = session.channel.id
        generation = _CHANNEL_TURN_GENERATIONS.get(channel_id, 0) + 1
        _CHANNEL_TURN_GENERATIONS[channel_id] = generation

        previous = _ACTIVE_CHANNEL_TURNS.get(channel_id)
        if previous is not None and not previous.task.done():
            previous.superseded = True
            previous.task.cancel()
            try:
                await previous.task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                _LOGGER.warning(f"superseded channel turn cleanup failed: {summarize_exception(exc)}")

        if _CHANNEL_TURN_GENERATIONS.get(channel_id) != generation:
            return BLOCK

        task = asyncio.create_task(handler(session, ctx))
        active = _ActiveChannelTurn(task=task, generation=generation)
        _ACTIVE_CHANNEL_TURNS[channel_id] = active
        try:
            return await task
        except asyncio.CancelledError:
            if active.superseded:
                return BLOCK
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            raise
        finally:
            if _ACTIVE_CHANNEL_TURNS.get(channel_id) is active:
                _ACTIVE_CHANNEL_TURNS.pop(channel_id, None)
            if _CHANNEL_TURN_GENERATIONS.get(channel_id) == generation:
                _CHANNEL_TURN_GENERATIONS.pop(channel_id, None)

    return wrapped
