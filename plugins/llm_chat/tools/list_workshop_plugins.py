"""Discover exact immutable workshop revisions inside the current chat scope."""

from __future__ import annotations

import json

from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from utils.plugin_workshop_core.models import WorkshopError

from ._workshop import WorkshopToolContext, workshop_metadata, authorized_workshop_actor
from ..core.types import JSONType
from ._registration import register_tool
from ..core.delivery import DeliveryError
from ..core.tool_trace import record_tool_evidence


def register_list_workshop_plugins(
    dispatcher: PluginDispatcher[JSONType], runtime: WorkshopToolContext
) -> Subscriber[JSONType]:
    async def list_workshop_plugins(plugin_name: str = "", limit: int = 10, offset: int = 0) -> str:
        """Find exact plugin versions for follow-up repairs without executing or approving them.

        Ordinary users see their own submissions in this scope; operators see this scope only.
        Supply plugin_name to page that project's versions. Use returned version and source_hash
        with read_workshop_plugin; old immutable versions remain available across chat turns.
        """
        actor, _ = authorized_workshop_actor()
        if type(limit) is not int or not 1 <= limit <= 50 or type(offset) is not int or not 0 <= offset < 2**63 - 50:
            raise DeliveryError("limit must be 1..50 and offset a nonnegative integer")
        if not isinstance(plugin_name, str):
            raise DeliveryError("plugin_name must be a string")
        assert actor.scope_id is not None
        try:
            records = await runtime.get_service().list_revisions(
                actor, query_scope=actor.scope_id, plugin_name=plugin_name, limit=limit + 1, offset=offset
            )
        except WorkshopError as exc:
            raise DeliveryError(f"Workshop read not allowed ({exc.code}): {exc}") from None
        except Exception as exc:
            runtime.warn(f"plugin workshop listing failed: {type(exc).__name__}")
            raise DeliveryError("Plugin workshop list is unavailable") from None
        items = [workshop_metadata(record) for record in records[:limit]]
        payload = {
            "plugins": items,
            "offset": offset,
            "next_offset": offset + limit if len(records) > limit else None,
            "returned_count": len(items),
        }
        record_tool_evidence(payload)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    return register_tool(dispatcher, list_workshop_plugins)
