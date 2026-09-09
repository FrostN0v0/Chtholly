"""Generation-scoped policy around Entari's native external tool runner."""

from __future__ import annotations

import json
from typing import Any, cast
import asyncio
from functools import wraps
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from collections.abc import Iterator

from arclet.entari import Session, plugin
from agno.run.agent import RunOutput
from arclet.letoderea import Contexts, Subscriber
from agno.models.message import Message
from agno.tools.function import Function
from arclet.entari.const import ITEM_SESSION
from agno.run.requirement import RunRequirement
from entari_plugin_llm.tools import _ToolPropagator, available_functions
import entari_plugin_llm.service as llm_service_module
from entari_plugin_llm.sessions import SessionInfo

from .core.errors import summarize_exception
from .core.delivery import (
    current_llm_chat_delivery,
    contains_internal_image_reference,
    contains_internal_participant_reference,
)
from .core.tool_trace import current_tool_trace, llm_chat_tool_execution_scope
from .image_edit_refs import current_image_edit_references
from .runtime_context import copy_llm_chat_context
from .core.model_audit import sanitize_audit_value
from .reaction_feedback import current_reaction_feedback
from .core.native_images import extract_native_images
from .model_audit_runtime import (
    AuditedLiteLLMClient,
    model_audit_scope,
    current_model_audit,
    install_model_http_audit,
)
from .core.tool_trace_policy import DeliverySnapshot

_MIN_TOOL_CALL_LIMIT = 8
_MAX_TOOL_CALL_LIMIT = 64
_UTILITY_TOOL_CALL_RESERVE = 3
_READ_ONLY_CONCURRENCY = 4
_ORDERED_DELIVERY_TOOLS = frozenset(
    {
        "capture_web_reference",
        "generate_image",
        "edit_image",
        "html2pic",
        "jinja2pic",
        "markdown2pic",
        "publish_web_preview",
        "revoke_web_preview",
        "screenshot_web_page",
        "send_audio",
        "send_artifact",
        "send_channel_image",
        "send_external_image",
        "send_image",
        "send_merged_forward",
        "send_text",
        "speak",
    }
)
_IMAGE_EDIT_BLOCKED_TOOLS = _ORDERED_DELIVERY_TOOLS - {"capture_web_reference", "edit_image", "revoke_web_preview"}
_READ_ONLY_TOOLS = frozenset(
    {
        "describe_channel_participant_avatar",
        "describe_channel_image",
        "find_channel_participants",
        "get_local_time",
        "list_image_resources",
        "list_tts_voices",
        "list_web_artifacts",
        "read_web_artifact",
        "read_channel_messages",
        "read_web_page",
        "web_search",
        "list_sessions",
        "read_session_handoff",
        "list_tool_executions",
        "read_agent_event",
        "read_tool_execution",
    }
)
_INTERNAL_PARTICIPANT_REFERENCE_TOOLS = {
    "describe_channel_participant_avatar",
    "describe_channel_image",
    "find_channel_participants",
    "read_channel_messages",
    "send_channel_image",
    "send_merged_forward",
    "send_text",
}
_INTERNAL_IMAGE_REFERENCE_TOOLS = {"edit_image"}
_DELIVERY_TOOL_LOCK: ContextVar[asyncio.Lock | None] = ContextVar("llm_chat_agno_delivery_tool_lock", default=None)


@dataclass
class _ToolBudget:
    limit: int
    used: int = 0


@dataclass
class _Invocation:
    subscriber: Subscriber[Any]
    error: BaseException | None = None
    result: object = None


@dataclass(frozen=True)
class _RequirementView:
    """Give the upstream runner the actual requirement, not a copied run."""

    active_requirements: tuple[RunRequirement, ...]


_TOOL_BUDGET: ContextVar[_ToolBudget | None] = ContextVar("llm_chat_agno_tool_budget", default=None)
_INVOCATION: ContextVar[_Invocation | None] = ContextVar("llm_chat_agno_tool_invocation", default=None)


@contextmanager
def agno_delivery_tool_scope() -> Iterator[None]:
    """Serialize side effects while leaving bounded read-only groups concurrent."""

    token = _DELIVERY_TOOL_LOCK.set(asyncio.Lock())
    try:
        yield
    finally:
        _DELIVERY_TOOL_LOCK.reset(token)


def recommended_tool_call_limit(web_calls: int, text_messages: int, media_messages: int) -> int:
    """Leave delivery and utility headroom beyond the generation web budget."""

    requested = (
        max(0, int(web_calls)) + max(0, int(text_messages)) + max(0, int(media_messages)) + _UTILITY_TOOL_CALL_RESERVE
    )
    return min(_MAX_TOOL_CALL_LIMIT, max(_MIN_TOOL_CALL_LIMIT, requested))


@contextmanager
def agno_tool_call_limit_scope(limit: int) -> Iterator[None]:
    """Count actual external calls across every pause in one generation attempt."""

    normalized = min(_MAX_TOOL_CALL_LIMIT, max(_MIN_TOOL_CALL_LIMIT, int(limit)))
    token = _TOOL_BUDGET.set(_ToolBudget(normalized))
    try:
        yield
    finally:
        _TOOL_BUDGET.reset(token)


def register_llm_chat_tool(subscriber: Subscriber[Any]) -> None:
    """Authorize a runtime dispatcher registration and retain source outcomes.

    The upstream Function, parameters and Subscriber DI remain authoritative.
    Only this subscriber instance is observed, only while its external call is
    active. Exceptions are re-raised unchanged for upstream result handling.
    """

    previous = subscriber.handle
    if _is_compat_wrapper(previous):
        return
    name = subscriber.__name__
    predecessor = available_functions.get(name)
    if predecessor is not None and predecessor[0] is subscriber:
        predecessor = None
    elif predecessor is not None and not _is_compat_wrapper(predecessor[0].handle):
        raise ValueError(f"LLM tool {name} is already owned by another plugin")
    if predecessor is not None:
        # Upstream's publisher skips existing names during Entari staged reload.
        # Reuse its schema generator, then guard its unconditional disposal pop.
        _ToolPropagator({"external_execution": True}).validate(subscriber)
    previous_dispose = subscriber.dispose
    setattr(subscriber, "__llm_chat_disposed__", False)

    @wraps(previous_dispose)
    def owned_dispose() -> Any:
        current = available_functions.get(name)
        try:
            return previous_dispose()
        finally:
            setattr(subscriber, "__llm_chat_disposed__", True)
            if current is not None and current[0] is not subscriber:
                available_functions[name] = current
            elif predecessor is not None and not getattr(predecessor[0], "__llm_chat_disposed__", False):
                available_functions[name] = predecessor

    setattr(subscriber, "dispose", owned_dispose)

    @wraps(previous)
    async def observed_handle(context: Contexts, inner: bool = False) -> Any:
        invocation = _INVOCATION.get()
        if invocation is None or invocation.subscriber is not subscriber:
            return await previous(context, inner=inner)
        try:
            invocation.result = await previous(context, inner=inner)
            return invocation.result
        except BaseException as exc:
            invocation.error = exc
            raise

    setattr(observed_handle, "__llm_chat_compat__", True)
    setattr(subscriber, "handle", observed_handle)

    def restore() -> None:
        if subscriber.handle is observed_handle:
            setattr(subscriber, "handle", previous)

    plugin.collect_disposes(restore)


def _authorized(name: str) -> bool:
    entry = available_functions.get(name)
    return entry is not None and _is_compat_wrapper(entry[0].handle) and entry[1].external_execution is True


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


async def _flush_tool_events(audit: Any, recorder: Any) -> None:
    if audit is None:
        return
    audit.record_tool_events(recorder.events)
    task = asyncio.create_task(audit.flush())
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


def _append_stopped_results(response: RunOutput, requirements: list[RunRequirement]) -> None:
    """Complete the real paused assistant/tool transcript before finalization."""

    if response.messages is None:
        response.messages = []
    existing = {message.tool_call_id for message in response.messages if message.role == "tool"}
    for requirement in requirements:
        execution = requirement.tool_execution
        if execution is None or execution.tool_call_id in existing:
            continue
        response.messages.append(
            Message(
                role="tool",
                content=execution.result,
                tool_call_id=execution.tool_call_id,
                tool_name=execution.tool_name,
                tool_args=execution.tool_args,
                tool_call_error=execution.tool_call_error,
                stop_after_tool_call=execution.stop_after_tool_call,
            )
        )
        existing.add(execution.tool_call_id)


async def _run_local_tools(
    runner: Any,
    response: RunOutput,
    llm_session: SessionInfo | None,
    session: Session | None,
    ctx: Contexts | None,
) -> bool:
    recorder = current_tool_trace()
    if recorder is None:
        return await runner(response, llm_session, session, ctx)
    context = copy_llm_chat_context()
    if context is None:
        context = Contexts(ctx.copy()) if ctx is not None else Contexts()
    inherited_session = context.get(ITEM_SESSION)
    if inherited_session is not None:
        session = inherited_session
    requirements = [req for req in response.active_requirements if req.needs_external_execution]
    budget = _TOOL_BUDGET.get() or _ToolBudget(_MIN_TOOL_CALL_LIMIT, used=len(recorder.events))
    remaining = max(0, budget.limit - budget.used)
    budget.used += min(remaining, len(requirements))
    exhausted = len(requirements) > remaining
    audit = current_model_audit()
    calls = []
    unsafe = []
    for req in requirements:
        execution = req.tool_execution
        name = execution.tool_name if execution is not None else ""
        arguments = execution.tool_args or {} if execution is not None else {}
        invalid_reference = (
            name not in _INTERNAL_PARTICIPANT_REFERENCE_TOOLS and contains_internal_participant_reference(arguments)
        ) or (name not in _INTERNAL_IMAGE_REFERENCE_TOOLS and contains_internal_image_reference(arguments))
        unsafe.append(invalid_reference)
        call = recorder.start(
            name or "unknown",
            {} if invalid_reference else arguments,
            tool_call_id=execution.tool_call_id or "" if execution is not None else "",
        )
        calls.append(call)
        if audit is not None:
            audit.record_tool_start(call)
    finished: set[int] = set()
    should_continue = True

    async def execute_one(index: int) -> None:
        nonlocal should_continue
        req = requirements[index]
        execution = req.tool_execution
        call = calls[index]
        name = call.tool_name
        before = _delivery_snapshot()
        reaction = current_reaction_feedback()
        try:
            if index >= remaining:
                error = f"Tool call limit reached. Generation budget exhausted after {budget.limit} calls."
                req.set_external_execution_result(error)
                if execution is not None:
                    execution.tool_call_error = True
                raise ValueError(error)
            if execution is None or not _authorized(name):
                raise ValueError("Tool is not allowed in this llm_chat generation")
            if unsafe[index]:
                raise ValueError("Invalid internal reference for this tool")
            subscriber, function = available_functions[name]
            arguments = execution.tool_args or {}
            if any(key not in function.parameters.get("properties", {}) for key in arguments) or any(
                key not in arguments for key in function.parameters.get("required", [])
            ):
                raise ValueError("Invalid arguments for this tool")
            references = current_image_edit_references()
            if (
                name in _IMAGE_EDIT_BLOCKED_TOOLS
                and references is not None
                and references.requires_image_edit
                and not references.edit_confirmed
            ):
                raise ValueError("Invalid delivery order: edit_image must complete before any other delivery tool")
            if reaction is not None:
                await reaction.tool_started(name)
            invocation = _Invocation(subscriber)
            token = _INVOCATION.set(invocation)
            try:
                with llm_chat_tool_execution_scope(call.execution_ref), model_audit_scope(None):
                    continued = await runner(cast(RunOutput, _RequirementView((req,))), llm_session, session, context)
            finally:
                _INVOCATION.reset(token)
            should_continue = should_continue and continued
            payload = json.loads(req.external_execution_result or "null")
            if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
                raise RuntimeError("External tool runner returned an invalid result")
            if payload["ok"] is False:
                error = invocation.error
                if error is None:
                    error = (
                        invocation.result
                        if isinstance(invocation.result, BaseException)
                        else RuntimeError(payload["error"])
                    )
                recorder.finish_error(call, error, before=before, after=_delivery_snapshot())
                req.external_execution_result = json.dumps(
                    {"ok": False, "error": summarize_exception(error)}, ensure_ascii=False
                )
                execution.result = req.external_execution_result
                finished.add(index)
                execution.tool_call_error = True
                if reaction is not None:
                    await reaction.tool_failed()
            else:
                recorder.finish_success(
                    call,
                    payload.get("data"),
                    before=before,
                    after=_delivery_snapshot(),
                    audit_snapshot=sanitize_audit_value(payload.get("data")),
                )
            finished.add(index)
        except asyncio.CancelledError:
            if index not in finished:
                recorder.finish_cancelled(call, before=before, after=_delivery_snapshot())
                finished.add(index)
            if req.needs_external_execution:
                req.set_external_execution_result(json.dumps({"ok": False, "error": "Tool execution was cancelled"}))
            if execution is not None:
                execution.tool_call_error = True
            raise
        except Exception as exc:
            if index not in finished:
                recorder.finish_error(call, exc, before=before, after=_delivery_snapshot())
                finished.add(index)
            if req.needs_external_execution:
                req.set_external_execution_result(
                    json.dumps({"ok": False, "error": summarize_exception(exc)}, ensure_ascii=False)
                )
            if execution is not None:
                execution.tool_call_error = True
            if reaction is not None:
                await reaction.tool_failed()
        finally:
            await _flush_tool_events(audit, recorder)

    try:
        if audit is not None:
            await audit.flush()
        index = 0
        while index < len(requirements):
            if calls[index].tool_name in _READ_ONLY_TOOLS:
                end = index + 1
                while (
                    end < len(requirements)
                    and end - index < _READ_ONLY_CONCURRENCY
                    and calls[end].tool_name in _READ_ONLY_TOOLS
                ):
                    end += 1
                tasks = [asyncio.create_task(execute_one(item)) for item in range(index, end)]
                group = asyncio.gather(*tasks)
                try:
                    await asyncio.shield(group)
                except BaseException:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    raise
                index = end
            else:
                lock = _DELIVERY_TOOL_LOCK.get()
                if lock is None:
                    await execute_one(index)
                else:
                    async with lock:
                        await execute_one(index)
                index += 1
    except asyncio.CancelledError:
        for index, req in enumerate(requirements):
            if index in finished:
                continue
            snapshot = _delivery_snapshot()
            recorder.finish_cancelled(calls[index], before=snapshot, after=snapshot)
            if req.needs_external_execution:
                req.set_external_execution_result(json.dumps({"ok": False, "error": "Tool execution was cancelled"}))
            if req.tool_execution is not None:
                req.tool_execution.tool_call_error = True
        raise
    finally:
        await _flush_tool_events(audit, recorder)
    if exhausted or not should_continue:
        _append_stopped_results(response, requirements)
        return False
    return True


def _is_compat_wrapper(value: object) -> bool:
    return bool(getattr(value, "__llm_chat_compat__", False))


def _wrap_litellm_model(previous_model: Any) -> Any:
    class NativeImageLiteLLM(previous_model):
        __llm_chat_compat__ = True
        __llm_chat_original__ = previous_model

        def get_client(self) -> Any:
            client = super().get_client()
            return AuditedLiteLLMClient(client) if current_model_audit() is not None else client

        def _parse_provider_response(self, response: Any, **kwargs: Any) -> Any:
            model_response = super()._parse_provider_response(response, **kwargs)
            if current_tool_trace() is not None:
                images = extract_native_images(response)
                if images:
                    model_response.images = list(images)
            return model_response

    NativeImageLiteLLM.__name__ = getattr(previous_model, "__name__", "LiteLLM")
    NativeImageLiteLLM.__qualname__ = getattr(previous_model, "__qualname__", "LiteLLM")
    return NativeImageLiteLLM


def install_agno_tool_bridge() -> None:
    """Install context-scoped upstream delegation, restored on plugin disposal."""

    plugin.collect_disposes(install_model_http_audit())
    names = ("get_agno_tools", "run_llm_tools", "LiteLLM", "Agent")
    previous = {name: getattr(llm_service_module, name) for name in names}
    original = {name: getattr(value, "__llm_chat_original__", value) for name, value in previous.items()}

    @wraps(original["get_agno_tools"])
    def authorized_tools() -> list[Function]:
        functions = original["get_agno_tools"]()
        if current_tool_trace() is None:
            return functions
        return [function for function in functions if _authorized(function.name)]

    @wraps(original["run_llm_tools"])
    async def run_tools(
        response: RunOutput,
        llm_session: SessionInfo | None = None,
        session: Session | None = None,
        ctx: Contexts | None = None,
    ) -> bool:
        return await _run_local_tools(original["run_llm_tools"], response, llm_session, session, ctx)

    @wraps(original["Agent"])
    def local_agent(*args: Any, **kwargs: Any) -> Any:
        if current_tool_trace() is not None:
            budget = _TOOL_BUDGET.get()
            kwargs["tool_call_limit"] = budget.limit if budget is not None else _MIN_TOOL_CALL_LIMIT
            # Entari continues by run_id; stateless Agents otherwise discard the
            # paused run. This cache belongs to this one Agent, not shared storage.
            kwargs["cache_session"] = True
        return original["Agent"](*args, **kwargs)

    replacements = {
        "get_agno_tools": authorized_tools,
        "run_llm_tools": run_tools,
        "LiteLLM": _wrap_litellm_model(original["LiteLLM"]),
        "Agent": local_agent,
    }
    for name, replacement in replacements.items():
        setattr(replacement, "__llm_chat_compat__", True)
        setattr(replacement, "__llm_chat_original__", original[name])
        setattr(replacement, "__llm_chat_disposed__", False)
        setattr(llm_service_module, name, replacement)

    def restore() -> None:
        for name, replacement in replacements.items():
            setattr(replacement, "__llm_chat_disposed__", True)
            if getattr(llm_service_module, name) is replacement:
                old = previous[name]
                setattr(
                    llm_service_module, name, original[name] if getattr(old, "__llm_chat_disposed__", False) else old
                )

    plugin.collect_disposes(restore)
