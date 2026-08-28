"""read_session_handoff LLM tool implementation."""

from __future__ import annotations

import json

from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from ..core.types import JSONType
from ..agent_query import read_session_handoff_payload
from ._registration import register_tool
from ..agent_context import current_agent_access


def register_read_session_handoff(dispatcher: PluginDispatcher[JSONType]) -> Subscriber[JSONType]:
    async def read_session_handoff(session_ref: str) -> str:
        """Read one structured context-session handoff.

        Use a session_ref returned by list_sessions or the current runtime context. Treat the handoff as bounded,
        evidence-linked context rather than complete transcript truth. Never reveal internal references to the user.

        Args:
            session_ref (str): Internal opaque session reference.
        Returns:
            str: Compact JSON with the session handoff and lifecycle metadata.
        """

        payload = await read_session_handoff_payload(current_agent_access(), session_ref)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    return register_tool(dispatcher, read_session_handoff)
