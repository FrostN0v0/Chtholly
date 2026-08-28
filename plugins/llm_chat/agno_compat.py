"""Compatibility bridge for the Agno-backed entari-plugin-llm tool adapter."""

from __future__ import annotations

import json
from typing import Any
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Iterator

from arclet.entari import plugin
from agno.tools.function import Function
from arclet.letoderea.context import generate_contexts
import entari_plugin_llm.service as llm_service_module
from arclet.letoderea.exceptions import ExitState, _ExitException
from entari_plugin_llm.tools.event import LLMToolEvent, tools, available_functions

from .core.errors import summarize_exception
from .core.delivery import current_llm_chat_delivery, contains_internal_participant_reference
from .core.tool_trace import current_tool_trace, llm_chat_tool_execution_scope
from .runtime_context import copy_llm_chat_context
from .core.native_images import extract_native_images
from .core.tool_trace_policy import DeliverySnapshot

_MIN_TOOL_CALL_LIMIT = 8
_MAX_TOOL_CALL_LIMIT = 64
_UTILITY_TOOL_CALL_RESERVE = 3
_TOOL_CALL_LIMIT: ContextVar[int] = ContextVar(
    "llm_chat_agno_tool_call_limit",
    default=_MIN_TOOL_CALL_LIMIT,
)
_INTERNAL_REFERENCE_TOOLS = {
    "describe_channel_participant_avatar",
    "describe_channel_image",
    "find_channel_participants",
    "read_channel_messages",
    "send_channel_image",
    "send_merged_forward",
    "send_text",
}


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


def _delivery_snapshot() -> DeliverySnapshot:
    state = current_llm_chat_delivery()
    if state is None:
        return DeliverySnapshot()
    return DeliverySnapshot(
        active=True,
        attempts=state.delivery_attempts,
        confirmed=state.confirmed_deliveries,
        confirmed_media=state.confirmed_media_deliveries,
    )


def _normalize_tool_data(response: object) -> object:
    if isinstance(response, ExitState):
        return "Conversation ended" if response is ExitState.stop else str(response)
    if isinstance(response, _ExitException):
        return response.args[0] if response.args else None
    if isinstance(response, (str, int, float, bool, list, dict, type(None))):
        return response
    return str(response)


def _build_agno_tool(name: str) -> Function:
    subscriber = available_functions[name]

    async def wrapper(**kwargs: Any) -> str:
        recorder = current_tool_trace()
        unsafe_reference = name not in _INTERNAL_REFERENCE_TOOLS and contains_internal_participant_reference(kwargs)
        call = recorder.start(name, {} if unsafe_reference else kwargs) if recorder is not None else None
        before = _delivery_snapshot()
        try:
            if unsafe_reference:
                raise ValueError("Invalid internal participant reference for this tool")
            tool_context = await generate_contexts(LLMToolEvent(), inherit_ctx=copy_llm_chat_context())
            tool_context.update(kwargs)
            with llm_chat_tool_execution_scope(call.execution_ref if call is not None else ""):
                response = await subscriber.handle(tool_context, inner=True)
            data = _normalize_tool_data(response)
            if recorder is not None and call is not None:
                recorder.finish_success(call, data, before=before, after=_delivery_snapshot())
            return json.dumps({"ok": True, "data": data}, ensure_ascii=False)
        except asyncio.CancelledError:
            if recorder is not None and call is not None:
                recorder.finish_cancelled(call, before=before, after=_delivery_snapshot())
            raise
        except Exception as exc:
            if recorder is not None and call is not None:
                recorder.finish_error(call, exc, before=before, after=_delivery_snapshot())
            return json.dumps({"ok": False, "error": summarize_exception(exc)}, ensure_ascii=False)

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
