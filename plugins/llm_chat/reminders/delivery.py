"""Generation-local durable boundaries for scheduled transport attempts."""

from __future__ import annotations

from typing import Any
from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Callable, Iterator, Awaitable

from .store import ReminderClaim, renew_claim, record_confirmed, record_external_started


class ReminderLeaseLost(RuntimeError):
    """A cancelled, expired, or superseded claim may not cross an external boundary."""


class ReminderReceiptUnknown(RuntimeError):
    """Transport returned, but its durable confirmation could not be established."""


_ACTIVE: ContextVar[ReminderClaim | None] = ContextVar("llm_chat_reminder_transport", default=None)


@contextmanager
def reminder_transport_scope(claim: ReminderClaim) -> Iterator[None]:
    token = _ACTIVE.set(claim)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


async def send_reminder_transport(send: Callable[[], Awaitable[Any]]) -> Any:
    """Persist transport entry before invoking native Session.send and each real receipt afterward."""
    claim = _ACTIVE.get()
    if claim is None:
        return await send()
    if not await renew_claim(claim):
        raise ReminderLeaseLost("reminder claim lost before transport")
    if not await record_external_started(claim, kind="transport"):
        raise ReminderLeaseLost("reminder claim revoked before transport")
    receipts = await send()
    if not receipts:
        raise ReminderReceiptUnknown("transport returned no confirmed receipt")
    for receipt in receipts:
        receipt_id = str(getattr(receipt, "id", "") or "")
        if not receipt_id or not await record_confirmed(claim, receipt_id=receipt_id):
            raise ReminderReceiptUnknown("confirmed transport receipt could not be persisted")
    return receipts
