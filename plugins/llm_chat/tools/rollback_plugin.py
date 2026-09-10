"""Restore only an exact approved plugin version previously active on this host."""

from __future__ import annotations

from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from ._workshop import WorkshopToolContext, change_plugin
from ..core.types import JSONType
from ._registration import register_tool


def register_rollback_plugin(
    dispatcher: PluginDispatcher[JSONType],
    runtime: WorkshopToolContext,
) -> Subscriber[JSONType]:
    async def rollback_plugin(plugin_name: str, version: int, source_hash: str) -> str:
        """Restore an operator-approved known-good version after an explicit current operator rollback request.

        Use an exact existing version and full digest, never guess a version or overwrite unrelated plugins.
        The target must have passed acceptance, been approved, and actually been active before. This changes
        CODE ONLY: it does not reverse sent messages, user data, external requests or database mutations.
        Do not claim rollback succeeded unless the tool returns a confirmed active version.

        Args:
            plugin_name (str): Exact stable plugin key, without a path or workshop_ prefix.
            version (int): Previously active, accepted and approved version number.
            source_hash (str): Full source-plus-manifest digest of the rollback target.
        Returns:
            str: Confirmed restored version or a real refusal/failure.
        """
        return await change_plugin(runtime, "rollback", plugin_name, version, source_hash)

    return register_tool(dispatcher, rollback_plugin)
