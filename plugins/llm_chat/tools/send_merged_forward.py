"""send_merged_forward LLM tool implementation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from collections.abc import Callable

from arclet.entari import Session
from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from ._delivery import build_forward_chain, send_forward_fallback
from ..core.types import JSONType
from ._registration import register_tool
from ..core.delivery import (
    DeliveryError,
    wait_for_delivery,
    mark_delivery_attempt,
    mark_delivery_success,
    normalize_delivery_delay,
    reserve_forward_messages,
)


@dataclass
class MergedForwardToolContext:
    """Mutable dependencies used by the merged-forward tool."""

    warn: Callable[[str], object]


def register_send_merged_forward(
    dispatcher: PluginDispatcher[JSONType],
    context: MergedForwardToolContext,
) -> Subscriber[JSONType]:
    """Register merged-forward delivery with deterministic text fallback."""

    async def send_merged_forward(
        session: Session,
        messages: list[str],
        delay_seconds: float | None = None,
    ) -> str:
        """Send one merged-forward message, with paced plain-text fallback when unavailable.

        Prefer this when the reply would usually exceed the send_text message budget or contains several long
        sections. Choose send_text or send_merged_forward before the first text delivery and never mix them.

        Args:
            messages (list[str]): Ordered visible text nodes without internal control markers.
            delay_seconds (float | None): Target interval from the previous confirmed or possibly confirmed delivery.
        Returns:
            str: Delivery result and final-response guidance.
        """

        delay = normalize_delivery_delay(delay_seconds)
        if type(messages) is not list or any(not isinstance(message, str) for message in messages):
            raise DeliveryError("messages must be a list of strings")
        delivery_state, normalized_messages = reserve_forward_messages(messages)

        if session.account.platform != "onebot":
            return await send_forward_fallback(session, delivery_state, normalized_messages, delay)

        await wait_for_delivery(delivery_state, delay)
        try:
            await session.send(build_forward_chain(normalized_messages))
        except asyncio.CancelledError:
            mark_delivery_attempt(delivery_state)
            raise
        except Exception as exc:
            mark_delivery_attempt(delivery_state)
            context.warn(f"merged forward failed; falling back to paced text: {type(exc).__name__}")
            return await send_forward_fallback(session, delivery_state, normalized_messages, delay)

        mark_delivery_success(delivery_state, normalized_messages)
        count = len(normalized_messages)
        return f"已发送包含 {count} 个节点的合并转发；不要在最终回复中重复，若无需补充只返回 [END_OF_RESPONSE]。"

    return register_tool(dispatcher, send_merged_forward)
