"""Lease, mode and feedback fences at each autonomous native send."""

from __future__ import annotations

from typing import Any
import asyncio
from datetime import datetime, timezone
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from collections.abc import Callable, Iterator, Awaitable

from . import store
from .types import ContactClaim
from ..core.delivery import DeliveryRejected


class CompanionLeaseLost(DeliveryRejected):
    """A revoked autonomous turn cannot cross another external boundary."""


class CompanionReceiptUnknown(DeliveryRejected):
    """No durable native receipt is available; this send must not be replayed."""


@dataclass(slots=True)
class _TransportState:
    claim: ContactClaim
    guard: Callable[[], Awaitable[bool]]
    clock: Callable[[], datetime]
    active: bool = True


_ACTIVE: ContextVar[_TransportState | None] = ContextVar("companion_transport", default=None)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@contextmanager
def companion_transport_scope(
    claim: ContactClaim,
    guard: Callable[[], Awaitable[bool]],
    *,
    clock: Callable[[], datetime] = _now,
) -> Iterator[None]:
    state = _TransportState(claim, guard, clock)
    token = _ACTIVE.set(state)
    try:
        yield
    finally:
        state.active = False
        _ACTIVE.reset(token)


async def send_companion_transport(send: Callable[[], Awaitable[Any]]) -> Any:
    state = _ACTIVE.get()
    if state is None:
        return await send()
    if not state.active or not await state.guard():
        raise CompanionLeaseLost("Autonomous contact is no longer current")
    if not state.active:
        raise CompanionLeaseLost("Autonomous generation closed during authorization")
    if not await store.record_transport_started(state.claim, now=state.clock()):
        raise CompanionLeaseLost("Autonomous contact was revoked before transport")
    if not state.active:
        raise CompanionLeaseLost("Autonomous generation closed before native transport")
    receipts = await send()
    identifiers = [str(getattr(receipt, "id", "") or "") for receipt in (receipts or ())]
    if not identifiers or any(not identifier for identifier in identifiers):
        raise CompanionReceiptUnknown("Autonomous transport returned no complete confirmed receipt")
    recorded = asyncio.create_task(store.record_receipts(state.claim, identifiers, now=state.clock()))
    try:
        await asyncio.shield(recorded)
    except asyncio.CancelledError:
        await recorded
        raise
    return receipts
