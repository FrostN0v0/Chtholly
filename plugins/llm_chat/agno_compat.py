"""Compatibility bridge for the Agno-backed entari-plugin-llm tool adapter."""

from __future__ import annotations

import json
from typing import Any
from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Iterator

from arclet.entari import plugin
from agno.tools.function import Function
from arclet.letoderea.context import generate_contexts
import entari_plugin_llm.service as llm_service_module
from arclet.letoderea.exceptions import ExitState, _ExitException
from entari_plugin_llm.tools.event import LLMToolEvent, tools, available_functions

from .runtime_context import copy_llm_chat_context

_MIN_TOOL_CALL_LIMIT = 8
_MAX_TOOL_CALL_LIMIT = 32
_UTILITY_TOOL_CALL_RESERVE = 3
_TOOL_CALL_LIMIT: ContextVar[int] = ContextVar(
    "llm_chat_agno_tool_call_limit",
    default=_MIN_TOOL_CALL_LIMIT,
)


def recommended_tool_call_limit(web_calls: int, text_messages: int, media_messages: int) -> int:
    """Leave delivery and utility headroom beyond the generation web budget."""

    requested = (
        max(0, int(web_calls)) + max(0, int(text_messages)) + max(0, int(media_messages)) + _UTILITY_TOOL_CALL_RESERVE
    )
    return min(_MAX_TOOL_CALL_LIMIT, max(_MIN_TOOL_CALL_LIMIT, requested))


@contextmanager
def agno_tool_call_limit_scope(limit: int) -> Iterator[None]:
    """Apply one generation-local Agno tool-call ceiling."""

    normalized = min(_MAX_TOOL_CALL_LIMIT, max(_MIN_TOOL_CALL_LIMIT, int(limit)))
    token = _TOOL_CALL_LIMIT.set(normalized)
    try:
        yield
    finally:
        _TOOL_CALL_LIMIT.reset(token)


def _build_agno_tool(name: str) -> Function:
    subscriber = available_functions[name]

    async def wrapper(**kwargs: Any) -> str:
        tool_context = await generate_contexts(LLMToolEvent(), inherit_ctx=copy_llm_chat_context())
        tool_context.update(kwargs)
        try:
            response = await subscriber.handle(tool_context, inner=True)
            if isinstance(response, ExitState):
                data = "Conversation ended" if response is ExitState.stop else str(response)
                return json.dumps({"ok": True, "data": data}, ensure_ascii=False)
            if isinstance(response, _ExitException):
                data = response.args[0] if response.args else None
                return json.dumps({"ok": True, "data": data}, ensure_ascii=False)
            if isinstance(response, (str, int, float, bool, list, dict, type(None))):
                return json.dumps({"ok": True, "data": response}, ensure_ascii=False)
            return json.dumps({"ok": True, "data": str(response)}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"ok": False, "error": repr(exc)}, ensure_ascii=False)

    schema = next(schema["function"] for schema in tools if schema["function"]["name"] == name)
    return Function(
        name=name,
        description=schema["description"],
        parameters=schema["parameters"],
        entrypoint=wrapper,
        skip_entrypoint_processing=True,
    )


def build_agno_tools() -> list[Function]:
    return [_build_agno_tool(name) for name in available_functions]


def install_agno_tool_bridge() -> None:
    previous_tools = llm_service_module.get_agno_tools
    previous_agent = llm_service_module.Agent
    if previous_tools is build_agno_tools and getattr(previous_agent, "__llm_chat_compat__", False):
        return

    def bounded_agent(*args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("tool_call_limit", _TOOL_CALL_LIMIT.get())
        return previous_agent(*args, **kwargs)

    setattr(bounded_agent, "__llm_chat_compat__", True)
    llm_service_module.get_agno_tools = build_agno_tools
    setattr(llm_service_module, "Agent", bounded_agent)

    def restore() -> None:
        if llm_service_module.get_agno_tools is build_agno_tools:
            llm_service_module.get_agno_tools = previous_tools
        if llm_service_module.Agent is bounded_agent:
            setattr(llm_service_module, "Agent", previous_agent)

    plugin.collect_disposes(restore)
