"""Activate only a reviewed, passed, exact native plugin version."""

from __future__ import annotations

from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from ._workshop import WorkshopToolContext, change_plugin
from ..core.types import JSONType
from ._registration import register_tool


def register_activate_plugin(
    dispatcher: PluginDispatcher[JSONType],
    runtime: WorkshopToolContext,
) -> Subscriber[JSONType]:
    async def activate_plugin(plugin_name: str, version: int, source_hash: str) -> str:
        """Activate the exact accepted and operator-approved version, only on the current operator's request.

        Use the name, version and full digest returned by submit_plugin or the authenticated workshop. The
        operator must have reviewed and approved this immutable version through WebUI or native commands;
        this tool CANNOT grant approval. A generated plugin runs with every privilege of the Bot process.
        Failed replacement preserves the last usable version when restoration succeeds. Report the actual
        outcome; activating code cannot undo old messages, external requests or database mutations.

        Args:
            plugin_name (str): Exact stable plugin key, without a path or workshop_ prefix.
            version (int): Exact immutable approved version number.
            source_hash (str): Full source-plus-manifest digest for that approved version.
        Returns:
            str: Confirmed active version or a real refusal/failure.
        """
        return await change_plugin(runtime, "activate", plugin_name, version, source_hash)

    return register_tool(dispatcher, activate_plugin)
