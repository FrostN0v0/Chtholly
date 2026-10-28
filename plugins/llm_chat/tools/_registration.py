"""Registration helpers shared by llm_chat tool modules."""

from __future__ import annotations

from types import FunctionType
from typing import cast

from arclet.entari import plugin, metadata
from arclet.letoderea import Subscriber, enter_if
from arclet.entari.plugin import PluginRole
from arclet.entari.plugin.model import Plugin, PluginDispatcher

from ..core.types import JSONType
from ..core.tool_trace import current_tool_trace


def create_tool_dispatcher(label: str) -> PluginDispatcher[JSONType]:
    """Keep tool execution independent of inbound filters, inside a live generation."""
    from entari_plugin_llm import LLMToolEvent  # entari: plugin

    @plugin.niche(label, config={})
    def owner(_owner: Plugin) -> None:
        metadata(name=label, role=PluginRole.UTILITY, description="Generation-local native LLM tools.")

    async def active_generation() -> bool:
        return current_tool_trace() is not None

    dispatcher = cast(PluginDispatcher[JSONType], owner.dispatch(LLMToolEvent))
    dispatcher.propagators.append(enter_if(active_generation))
    return dispatcher


def register_tool(
    dispatcher: PluginDispatcher[JSONType],
    function: FunctionType,
) -> Subscriber[JSONType]:
    """Register a tool under the owning runtime module for safe disposal."""

    function.__module__ = dispatcher.plugin.module.__name__
    return cast(Subscriber[JSONType], dispatcher(function))
