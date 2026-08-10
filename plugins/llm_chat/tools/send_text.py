"""send_text LLM tool implementation."""

from __future__ import annotations

from arclet.entari import Session
from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from ._delivery import send_with_delivery
from ..core.types import JSONType
from ._registration import register_tool
from ..core.delivery import DeliveryError, reserve_text_message, normalize_delivery_delay


def register_send_text(dispatcher: PluginDispatcher[JSONType]) -> Subscriber[JSONType]:
    """Register the paced plain-text delivery tool."""

    async def send_text(session: Session, text: str, delay_seconds: float | None = None) -> str:
        """Send one paced text message as one visible chat bubble during the current llm_chat generation.

        Prefer this when a reply has two or more naturally separate chat beats, including factual answers with a
        conclusion followed by a reason, caveat, or follow-up.
        Use final response text only for one short self-contained bubble or content that should remain intact.
        Call once per beat, choose send_text or send_merged_forward before the first text delivery, and never mix them.

        Args:
            text (str): One complete visible text segment without internal control markers.
            delay_seconds (float | None): Target interval from the previous confirmed or possibly confirmed delivery.
        Returns:
            str: Delivery result and final-response guidance.
        """

        delay = normalize_delivery_delay(delay_seconds)
        delivery_state, normalized_text = reserve_text_message(text)
        try:
            await send_with_delivery(
                session,
                normalized_text,
                delivery_state,
                delay_seconds=delay,
                texts=[normalized_text],
            )
        except Exception as exc:
            raise DeliveryError(f"send_text delivery failed: {type(exc).__name__}") from None
        return "已发送 1 条文本消息；不要在最终回复中重复，若无需补充只返回 [END_OF_RESPONSE]。"

    return register_tool(dispatcher, send_text)
