"""read_agent_event LLM tool implementation."""

from __future__ import annotations

import json

from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from ..core.types import JSONType
from ..agent_query import read_event_payload
from ._registration import register_tool
from ..agent_context import current_agent_access


def register_read_agent_event(
    dispatcher: PluginDispatcher[JSONType],
    *,
    maximum_chars: int,
) -> Subscriber[JSONType]:
    async def read_agent_event(
        event_ref: str,
        path: str = "",
        offset: int = 0,
    ) -> str:
        """Read compact metadata or an explicitly requested stored field from one prior AgentEvent.

        Without path this returns only compact context-safe data. Read a path such as arguments.html or result.content
        only when the current user explicitly refers to that prior page, source, or result. Use offset for another
        bounded text segment. Never reveal event_ref, paths, or storage details to the user.

        Args:
            event_ref (str): Internal opaque event reference from session context or execution listing.
            path (str): Optional dot path into the stored payload.
            offset (int): Character offset for a large string field.
        Returns:
            str: Bounded JSON event data and continuation metadata.
        """

        payload = await read_event_payload(
            current_agent_access(),
            event_ref=event_ref,
            path=path,
            offset=offset if type(offset) is int else 0,
            max_chars=max(256, maximum_chars),
        )
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    return register_tool(dispatcher, read_agent_event)
