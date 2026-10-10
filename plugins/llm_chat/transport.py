"""Native sends with generation-local durable external-effect fences."""

from __future__ import annotations

from typing import Any
from collections.abc import Callable, Awaitable


async def send_turn_transport(
    send: Callable[[], Awaitable[Any]],
    *,
    on_confirmed: Callable[[], None] | None = None,
) -> Any:
    from .companion.delivery import send_companion_transport
    from .reminders.delivery import send_reminder_transport

    async def observed_send() -> Any:
        receipts = await send()
        if receipts and on_confirmed is not None:
            # Observe actual acknowledgement before a cancellable receipt-store
            # commit, so cancellation cannot erase an already delivered prefix.
            on_confirmed()
        return receipts

    return await send_companion_transport(lambda: send_reminder_transport(observed_send))
