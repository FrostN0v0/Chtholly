"""Shared delivery primitives for llm_chat tools."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence

from arclet.entari import Session, MessageChain

from ..steering import check_steer_boundary
from ..core.delivery import (
    DeliveryError,
    DeliveryState,
    wait_for_delivery,
    mark_delivery_attempt,
    mark_delivery_success,
)
from ..group_delivery import send_group_delivery


async def send_with_delivery(
    session: Session,
    payload: str | MessageChain,
    state: DeliveryState | None,
    *,
    delay_seconds: float | None = None,
    texts: Sequence[str] = (),
    media: bool | int = False,
    text_message: bool | None = None,
    before_start: Callable[[], None] | None = None,
) -> None:
    """Send one payload while recording delivery attempts and confirmations."""

    if await send_group_delivery(
        session,
        payload,
        state,
        delay_seconds=delay_seconds,
        texts=texts,
        media=media,
        text_message=text_message,
        before_start=before_start,
    ):
        return
    if state is not None:
        await wait_for_delivery(state, delay_seconds)
    started = False
    confirmed = False

    def acknowledge() -> None:
        nonlocal confirmed
        confirmed = True
        if state is not None:
            mark_delivery_success(state, texts, media=media, text_message=text_message)

    async def native_send():
        nonlocal started
        check_steer_boundary()
        if before_start is not None:
            before_start()
        started = True
        return await session.send(payload)

    try:
        from ..transport import send_turn_transport

        receipts = await asyncio.wait_for(send_turn_transport(native_send, on_confirmed=acknowledge), timeout=30.0)
        if not receipts:
            raise DeliveryError("Transport returned no confirmed delivery receipt")
    except asyncio.CancelledError:
        if started and not confirmed and state is not None:
            mark_delivery_attempt(state)
        raise
    except Exception:
        if started and not confirmed and state is not None:
            mark_delivery_attempt(state)
        raise
