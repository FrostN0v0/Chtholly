"""Recheck the live companion scope at the actual transport boundary."""

from __future__ import annotations

from typing import Any
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from collections.abc import Callable, Iterator, Awaitable

from ..core.delivery import DeliveryRejected


@dataclass(slots=True)
class _Fence:
    valid: Callable[[], Awaitable[bool]]
    active: bool = True


_CURRENT: ContextVar[_Fence | None] = ContextVar("companion_dialogue_transport", default=None)


@contextmanager
def dialogue_transport_scope(valid: Callable[[], Awaitable[bool]]) -> Iterator[None]:
    fence = _Fence(valid)
    token = _CURRENT.set(fence)
    try:
        yield
    finally:
        fence.active = False
        _CURRENT.reset(token)


async def send_dialogue_transport(send: Callable[[], Awaitable[Any]]) -> Any:
    fence = _CURRENT.get()
    if fence is not None:
        if not fence.active or not await fence.valid() or not fence.active:
            raise DeliveryRejected("Companion dialogue scope is no longer current")
    return await send()
