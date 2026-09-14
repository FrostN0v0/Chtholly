"""send_merged_forward LLM tool implementation."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable

from arclet.entari import Session
from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from ._delivery import send_merged_text
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

        Use this for a long answer with many points or substantial sections that would crowd the chat.
        Preserve each readable paragraph or point as a separate ordered node, not one wall of text.
        A few short paragraphs belong in send_text; a short answer can stay in final text.
        Plan the whole reply before the first text send; do not wait until the bubble budget is exhausted.
        Never mix send_text and send_merged_forward in one generation or add a redundant summary afterward.

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

        return await send_merged_text(
            session, delivery_state, normalized_messages, warn=context.warn, delay_seconds=delay
        )

    return register_tool(dispatcher, send_merged_forward)
