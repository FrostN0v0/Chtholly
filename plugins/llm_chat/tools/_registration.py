"""Registration helpers shared by llm_chat tool modules."""

from __future__ import annotations

from types import FunctionType
from typing import cast

from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from ..core.types import JSONType


def register_tool(
    dispatcher: PluginDispatcher[JSONType],
    function: FunctionType,
) -> Subscriber[JSONType]:
    """Register a tool under the owning runtime module for safe disposal."""

    function.__module__ = dispatcher.plugin.module.__name__
    return cast(Subscriber[JSONType], dispatcher(function))
