"""send_merged_forward LLM tool implementation."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable

from arclet.entari import Session
from arclet.letoderea import Subscriber
from satori.exception import ApiNotAvailable, MethodNotAllowedException
from arclet.entari.plugin.model import PluginDispatcher

from ._delivery import send_with_delivery, build_forward_chain, send_forward_fallback
from ..core.types import JSONType
from ._registration import register_tool
from ..core.delivery import (
    DeliveryError,
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

        try:
            await send_with_delivery(
                session,
                build_forward_chain(normalized_messages),
                delivery_state,
                delay_seconds=delay,
                texts=normalized_messages,
            )
        except (NotImplementedError, ApiNotAvailable, MethodNotAllowedException) as exc:
            context.warn(f"merged forward unavailable; falling back to paced text: {type(exc).__name__}")
            return await send_forward_fallback(session, delivery_state, normalized_messages, delay)

        count = len(normalized_messages)
        return f"已发送包含 {count} 个节点的合并转发；不要在最终回复中重复，若无需补充只返回 [END_OF_RESPONSE]。"

    return register_tool(dispatcher, send_merged_forward)
