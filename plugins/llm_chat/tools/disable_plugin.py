"""Disable a native workshop plugin with confirmed lifecycle evidence."""

from __future__ import annotations

import json

from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from utils.plugin_workshop_core.models import ProjectState, WorkshopError

from ._workshop import WorkshopToolContext, authorized_workshop_actor
from ..core.types import JSONType
from ._registration import register_tool
from ..core.delivery import DeliveryError
from ..core.tool_trace import record_tool_evidence


def disabled_evidence(state: ProjectState) -> None:
    if not state.enabled:
        record_tool_evidence(
            {
                "workshop_effect": "disabled",
                "plugin_name": state.plugin_name,
                "active_version": state.active_version,
                "enabled": False,
                "confirmed": True,
            }
        )


def register_disable_plugin(
    dispatcher: PluginDispatcher[JSONType], runtime: WorkshopToolContext
) -> Subscriber[JSONType]:
    async def disable_plugin(plugin_name: str) -> str:
        """Disable a current-scope workshop plugin for the current superuser's task.

        Choose disabling autonomously when the task requires stopping a plugin; no human approval
        or trigger phrase is needed. Use an exact plugin_name from workshop results. This unloads
        native code and preserves immutable versions, reports and data; it cannot retract messages,
        external requests or earlier data changes. Only a confirmed disabled result proves success.
        """
        actor = authorized_workshop_actor()
        if not isinstance(plugin_name, str) or not plugin_name:
            raise DeliveryError("An exact plugin_name is required")
        assert actor.scope_id is not None
        try:
            service = runtime.get_service()
            records = await service.list_revisions(actor, query_scope=actor.scope_id, plugin_name=plugin_name, limit=1)
            if not records:
                raise WorkshopError("The plugin is unavailable in the current scope", code="not_found", status=404)
            if authorized_workshop_actor() != actor:
                raise DeliveryError("The plugin workshop generation identity changed before disabling")
            state = await service.disable(plugin_name, actor, on_changed=disabled_evidence)
        except WorkshopError as exc:
            raise DeliveryError(f"Workshop operation not allowed ({exc.code}): {exc}") from None
        except DeliveryError:
            raise
        except Exception as exc:
            runtime.warn(f"plugin workshop disable failed: {type(exc).__name__}")
            raise DeliveryError(
                "Plugin workshop disabling failed; inspect its authenticated management report"
            ) from None
        return json.dumps(
            {
                "plugin_name": state.plugin_name,
                "active_version": state.active_version,
                "enabled": state.enabled,
                "confirmed": not state.enabled,
                "workshop_effect": "disabled" if not state.enabled else "none",
                "warning": "Disabling code does not undo messages already sent or data already modified.",
            },
            separators=(",", ":"),
        )

    return register_tool(dispatcher, disable_plugin)
