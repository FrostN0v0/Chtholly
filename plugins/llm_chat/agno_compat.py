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
from .core.native_images import extract_native_images

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


setattr(build_agno_tools, "__llm_chat_compat__", True)


def _is_compat_wrapper(value: object) -> bool:
    return bool(getattr(value, "__llm_chat_compat__", False))


def _wrap_litellm_model(previous_model: Any) -> Any:
    if _is_compat_wrapper(previous_model):
        return previous_model

    class NativeImageLiteLLM(previous_model):
        __llm_chat_compat__ = True
        __llm_chat_original__ = previous_model

        def _parse_provider_response(self, response: Any, **kwargs: Any) -> Any:
            model_response = super()._parse_provider_response(response, **kwargs)
            images = extract_native_images(response)
            if images:
                model_response.images = list(images)
            return model_response

    NativeImageLiteLLM.__name__ = getattr(previous_model, "__name__", "LiteLLM")
    NativeImageLiteLLM.__qualname__ = getattr(previous_model, "__qualname__", "LiteLLM")
    return NativeImageLiteLLM


def install_agno_tool_bridge() -> None:
    previous_tools = llm_service_module.get_agno_tools
    previous_agent = llm_service_module.Agent
    previous_model = llm_service_module.LiteLLM
    if _is_compat_wrapper(previous_tools) and _is_compat_wrapper(previous_agent) and _is_compat_wrapper(previous_model):
        original_tools = getattr(previous_tools, "__llm_chat_original__", previous_tools)
        original_agent = getattr(previous_agent, "__llm_chat_original__", previous_agent)
        original_model = getattr(previous_model, "__llm_chat_original__", previous_model)

        def restore_existing() -> None:
            if llm_service_module.get_agno_tools is previous_tools:
                llm_service_module.get_agno_tools = original_tools
            if llm_service_module.Agent is previous_agent:
                setattr(llm_service_module, "Agent", original_agent)
            if llm_service_module.LiteLLM is previous_model:
                setattr(llm_service_module, "LiteLLM", original_model)

        plugin.collect_disposes(restore_existing)
        return

    original_agent = getattr(previous_agent, "__llm_chat_original__", previous_agent)

    def bounded_agent(*args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("tool_call_limit", _TOOL_CALL_LIMIT.get())
        return original_agent(*args, **kwargs)

    setattr(bounded_agent, "__llm_chat_compat__", True)
    setattr(build_agno_tools, "__llm_chat_original__", previous_tools)
    setattr(bounded_agent, "__llm_chat_original__", original_agent)
    wrapped_model = _wrap_litellm_model(previous_model)

    setattr(llm_service_module, "get_agno_tools", build_agno_tools)
    setattr(llm_service_module, "Agent", bounded_agent)
    setattr(llm_service_module, "LiteLLM", wrapped_model)

    def restore() -> None:
        if llm_service_module.get_agno_tools is build_agno_tools:
            llm_service_module.get_agno_tools = previous_tools
        if llm_service_module.Agent is bounded_agent:
            setattr(llm_service_module, "Agent", previous_agent)
        if llm_service_module.LiteLLM is wrapped_model:
            setattr(llm_service_module, "LiteLLM", previous_model)

    plugin.collect_disposes(restore)
