"""list_tool_executions LLM tool implementation."""

from __future__ import annotations

import json

from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from ..core.types import JSONType
from ..agent_query import list_tool_executions_payload
from ._registration import register_tool
from ..agent_context import current_agent_access


def register_list_tool_executions(dispatcher: PluginDispatcher[JSONType]) -> Subscriber[JSONType]:
    async def list_tool_executions(session_ref: str = "", limit: int = 20) -> str:
        """List tool executions from the current or explicitly authorized archived context session.

        Use this to locate a prior execution before reading a specific result. Empty session_ref selects the current
        session. Internal references must not be exposed to the user.

        Args:
            session_ref (str): Optional opaque session reference returned by list_sessions.
            limit (int): Maximum recent executions to return, capped at 50.
        Returns:
            str: Compact JSON execution metadata without large payloads.
        """

        requested = limit if type(limit) is int else 20
        payload = await list_tool_executions_payload(
            current_agent_access(),
            session_ref=session_ref,
            limit=min(max(1, requested), 50),
        )
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    return register_tool(dispatcher, list_tool_executions)
