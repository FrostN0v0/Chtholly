"""Shared delivery primitives for llm_chat tools."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from satori import Text, Message
from arclet.entari import Session, MessageChain

from ..core.delivery import (
    DeliveryError,
    DeliveryState,
    wait_for_delivery,
    mark_delivery_attempt,
    mark_delivery_success,
)


async def send_with_delivery(
    session: Session,
    payload: str | MessageChain,
    state: DeliveryState | None,
    *,
    delay_seconds: float | None = None,
    texts: Sequence[str] = (),
    media: bool = False,
) -> None:
    """Send one payload while recording delivery attempts and confirmations."""

    if state is not None:
        await wait_for_delivery(state, delay_seconds)
    try:
        await session.send(payload)
    except asyncio.CancelledError:
        if state is not None:
            mark_delivery_attempt(state)
        raise
    except Exception:
        if state is not None:
            mark_delivery_attempt(state)
        raise
    if state is not None:
        mark_delivery_success(state, texts, media=media)


def build_forward_chain(messages: Sequence[str]) -> MessageChain:
    """Build one OneBot-compatible merged-forward chain."""

    forward = Message(
        forward=True,
        content=[Message(content=[Text(text)]) for text in messages],
    )
    return MessageChain([forward])


async def send_forward_fallback(
    session: Session,
    state: DeliveryState,
    messages: Sequence[str],
    delay_seconds: float | None,
) -> str:
    """Send a merged-forward payload as paced text with prefix-aware failures."""

    total = len(messages)
    for index, text in enumerate(messages):
        await wait_for_delivery(state, delay_seconds)
        try:
            await session.send(text)
        except asyncio.CancelledError:
            mark_delivery_attempt(state)
            raise
        except Exception:
            mark_delivery_attempt(state)
            raise DeliveryError(
                f"merged forward fallback confirmed {index}/{total} text messages before failure; "
                "do not repeat the confirmed prefix"
            ) from None
        mark_delivery_success(state, [text])
    return (
        f"合并转发不可用，已按顺序回退发送 {total} 条普通文本；"
        "不要在最终回复中重复，若无需补充只返回 [END_OF_RESPONSE]。"
    )
