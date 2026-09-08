"""pin_context LLM tool implementation."""

from __future__ import annotations

import json

from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from ..core.types import JSONType
from ..agent_query import pin_context_payload
from ._registration import register_tool
from ..agent_context import current_agent_access


def register_pin_context(dispatcher: PluginDispatcher[JSONType]) -> Subscriber[JSONType]:
    async def pin_context(event_ref: str, label: str) -> str:
        """Pin a prior event as a cross-session context anchor after explicit user instruction.

        Call only when the current user clearly asks to remember or preserve that specific context for later sessions.
        Pinning does not write profile facts or long-term episodic memory. Never reveal internal references to users.

        Args:
            event_ref (str): Internal opaque event reference to preserve.
            label (str): Short semantic label describing why the event remains relevant.
        Returns:
            str: Compact JSON confirmation of the context anchor.
        """

        payload = await pin_context_payload(
            current_agent_access(),
            event_ref=event_ref,
            label=label,
        )
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    return register_tool(dispatcher, pin_context)
