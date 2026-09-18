"""prepare_merged_forward LLM tool implementation."""

from __future__ import annotations

from satori import Text, Message
from arclet.entari import Session
from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from ..core.media import has_meaningful_text
from ..core.types import JSONType
from ._registration import register_tool
from ..core.delivery import DeliveryError, clean_delivery_fragment, require_llm_chat_delivery
from ..prepared_media import prepare_media


def register_prepare_merged_forward(
    dispatcher: PluginDispatcher[JSONType],
) -> Subscriber[JSONType]:
    """Register native merged-forward preparation without transport or fallback."""

    async def prepare_merged_forward(session: Session, messages: list[str]) -> dict[str, JSONType]:
        """Prepare ordered plain-text nodes as one merged-forward message.

        This OneBot-only tool returns a media_ref without sending any message.
        Pass that reference as the only media segment in one send_msg call.
        Node attribution belongs to the runtime bot, never model-selected
        authors or platform IDs. Unsupported platforms fail without splitting
        the nodes into separate messages or sending a text fallback.

        Args:
            messages (list[str]): Ordered visible plain-text nodes without internal control markers.
        """

        state = require_llm_chat_delivery()
        if session.account.platform != "onebot":
            raise DeliveryError("merged forwards are unavailable on this platform")
        if type(messages) is not list or not messages:
            raise DeliveryError("messages must be a non-empty list of strings")
        if len(messages) > state.limits.max_forward_nodes:
            raise DeliveryError("merged forward exceeds the node limit")

        normalized_messages: list[str] = []
        for message in messages:
            text = clean_delivery_fragment(message, field="messages")
            if not has_meaningful_text(text):
                raise DeliveryError("merged forward nodes must contain meaningful text")
            if len(text) > state.limits.max_forward_chars_per_node:
                raise DeliveryError("merged forward exceeds the per-node character limit")
            normalized_messages.append(text)
        history_marker = "\n\n".join(normalized_messages)
        if len(history_marker) > state.limits.max_total_text_chars:
            raise DeliveryError("merged forward exceeds the total character limit")

        # Unattributed native nodes use the transport's authenticated bot author.
        forward = Message(
            forward=True,
            content=[Message(content=[Text(text)]) for text in normalized_messages],
        )
        result = prepare_media(
            session,
            forward,
            byte_count=len(history_marker.encode("utf-8")),
            tool_name="prepare_merged_forward",
            history_marker=history_marker,
            metadata={"node_count": len(normalized_messages)},
        )
        result["node_count"] = len(normalized_messages)
        return result

    return register_tool(dispatcher, prepare_merged_forward)


__all__ = ["register_prepare_merged_forward"]
