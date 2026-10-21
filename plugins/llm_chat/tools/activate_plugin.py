"""Activate an exact functionally accepted native plugin version."""

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
        """Activate an exact accepted version for the current superuser's plugin task.

        After submit_plugin passes acceptance, inspect its feedback and choose activation autonomously;
        no human review, approval record or activation phrase is required. Use the exact name, version
        and full digest returned by submission or workshop reads, never guessed values. A generated
        plugin runs with every privilege of the Bot process; acceptance is not a security certificate.
        Failed replacement preserves the last usable version when restoration succeeds. Report the
        actual outcome; changing code cannot undo old messages, external requests or data mutations.

        Args:
            plugin_name (str): Exact stable plugin key, without a path or workshop_ prefix.
            version (int): Exact immutable version number with passed acceptance.
            source_hash (str): Full source-plus-manifest digest for that version.
        Returns:
            str: Confirmed active version or a real refusal/failure.
        """
        return await change_plugin(runtime, "activate", plugin_name, version, source_hash)

    return register_tool(dispatcher, activate_plugin)
