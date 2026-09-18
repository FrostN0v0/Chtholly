"""Shared delivery primitives for llm_chat tools."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from arclet.entari import Session, MessageChain

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
) -> None:
    """Send one payload while recording delivery attempts and confirmations."""

    if await send_group_delivery(
        session, payload, state, delay_seconds=delay_seconds, texts=texts, media=media, text_message=text_message
    ):
        return
    if state is not None:
        await wait_for_delivery(state, delay_seconds)
    try:
        receipts = await asyncio.wait_for(session.send(payload), timeout=30.0)
        if not receipts:
            raise DeliveryError("Transport returned no confirmed delivery receipt")
    except asyncio.CancelledError:
        if state is not None:
            mark_delivery_attempt(state)
        raise
    except Exception:
        if state is not None:
            mark_delivery_attempt(state)
        raise
    if state is not None:
        mark_delivery_success(state, texts, media=media, text_message=text_message)
