"""read_tool_execution LLM tool implementation."""

from __future__ import annotations

import json

from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from ..core.types import JSONType
from ..agent_query import read_tool_execution_payload
from ._registration import register_tool
from ..agent_context import current_agent_access


def register_read_tool_execution(
    dispatcher: PluginDispatcher[JSONType],
    *,
    maximum_chars: int,
) -> Subscriber[JSONType]:
    async def read_tool_execution(
        execution_ref: str,
        path: str = "",
        offset: int = 0,
    ) -> str:
        """Read one prior tool execution by its internal execution reference.

        Use path to retrieve a stored field only when the current user explicitly refers to that prior source or
        result. Prefer compact metadata first, then request a narrow path. Never expose internal references to users.

        Args:
            execution_ref (str): Opaque reference returned by list_tool_executions.
            path (str): Optional dot path such as arguments.html or result.
            offset (int): Character offset for another bounded string segment.
        Returns:
            str: Bounded JSON execution data and continuation metadata.
        """

        payload = await read_tool_execution_payload(
            current_agent_access(),
            execution_ref=execution_ref,
            path=path,
            offset=offset if type(offset) is int else 0,
            max_chars=max(256, maximum_chars),
        )
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    return register_tool(dispatcher, read_tool_execution)
