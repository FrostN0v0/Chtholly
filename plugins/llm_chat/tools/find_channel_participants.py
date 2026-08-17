"""find_channel_participants LLM tool implementation."""

from __future__ import annotations

import json

from arclet.entari import Session
from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from ..core.types import JSONType
from ..perception import PerceptionProvider
from ._registration import register_tool


def register_find_channel_participants(
    dispatcher: PluginDispatcher[JSONType],
    get_perception: PerceptionProvider,
) -> Subscriber[JSONType]:
    """Register the read-only participant search tool."""

    async def find_channel_participants(
        query: str = "",
        limit: int = 5,
        *,
        session: Session,
    ) -> str:
        """Find recent participants in the current public channel.

        Use this to resolve a display name or earlier nickname into an opaque participant_ref before filtering message
        history or describing an avatar. An empty query lists the most recently active participants. Results are
        current-channel read-only data and may be incomplete. Never reveal participant_ref values or internal fields.

        Args:
            query (str): Display name, nickname, group card, or participant_ref to match. Defaults to empty.
            limit (int): Maximum matches, clamped to 1-10. Defaults to 5.
        Returns:
            str: Compact JSON containing bounded current-channel participant matches.
        """

        normalized_query = query.strip() if isinstance(query, str) else ""
        normalized_limit = limit if type(limit) is int else 5
        rows = await get_perception().find_participants(
            session,
            normalized_query,
            limit=min(10, max(1, normalized_limit)),
        )
        return json.dumps(
            {"participants": rows},
            ensure_ascii=False,
            separators=(",", ":"),
        )

    return register_tool(dispatcher, find_channel_participants)
