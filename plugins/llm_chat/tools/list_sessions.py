"""list_sessions LLM tool implementation."""

from __future__ import annotations

import json

from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from ..core.types import JSONType
from ..agent_query import list_sessions_payload
from ._registration import register_tool
from ..agent_context import current_agent_access


def register_list_sessions(
    dispatcher: PluginDispatcher[JSONType],
    *,
    maximum: int,
) -> Subscriber[JSONType]:
    async def list_sessions(limit: int = 10) -> str:
        """List archived context sessions for this chat scope.

        Call only when the current user explicitly asks about an earlier or previous session. Session references are
        internal lookup handles and must never be quoted to the user.

        Args:
            limit (int): Maximum recent sessions to list.
        Returns:
            str: Compact JSON metadata for sessions in the current chat scope.
        """

        requested = limit if type(limit) is int else 10
        payload = await list_sessions_payload(
            current_agent_access(),
            limit=min(max(1, requested), max(1, maximum)),
        )
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    return register_tool(dispatcher, list_sessions)
