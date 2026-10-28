"""Apply admitted inputs only between complete native model/tool steps."""

from __future__ import annotations

from typing import Any
from contextvars import ContextVar
from collections.abc import Callable, Awaitable

from agno.models.message import Message

from .steering import SteeringState, current_steering
from .context_builder import estimate_tokens
from .core.tool_trace import current_tool_trace, current_tool_execution_ref
from .model_audit_runtime import current_model_audit
from .native_image_delivery import discard_pending_native_images

_APPLYING: ContextVar[bool] = ContextVar("llm_chat_applying_steer", default=False)


def _request_steering() -> SteeringState | None:
    if _APPLYING.get() or current_tool_trace() is None or current_tool_execution_ref():
        return None
    return current_steering()


def steer_request_parameters(parameters: dict[str, Any]) -> dict[str, Any]:
    state = _request_steering()
    if state is not None and state.media_requested and state.media_request_timeout is not None:
        parameters.update(timeout=state.media_request_timeout, max_retries=0, parallel_tool_calls=False)
    return parameters


async def apply_model_steering(
    messages: list[Message],
    *,
    format_messages: Callable[[list[Message]], list[dict[str, Any]]] | None = None,
) -> None:
    state = _request_steering()
    if state is None:
        return
    token = _APPLYING.set(True)
    try:
        if state.on_request is not None and format_messages is not None:
            state.on_request(format_messages(messages))
        additions = await state.drain()
        messages.extend(Message.model_validate(message) for message in additions)
        if additions:
            recorder = current_model_audit()
            if recorder is not None:
                recorder.append(
                    "steer_checkpoint",
                    model_visible=False,
                    payload={"applied_revision": state.applied_revision, "status": "applied"},
                )
                await recorder.flush()
    finally:
        _APPLYING.reset(token)


async def invoke_with_steering(
    invoke: Callable[..., Awaitable[Any]],
    messages: list[Message],
    *args: Any,
    format_messages: Callable[[list[Message]], list[dict[str, Any]]],
    **kwargs: Any,
) -> Any:
    state = _request_steering()
    if state is None:
        return await invoke(messages, *args, **kwargs)
    while True:
        await apply_model_steering(messages, format_messages=format_messages)
        if state.applied_revision > 1 and state.request_input_budget > 0:
            tokens = estimate_tokens(state.request_model_name, format_messages(messages))
            if tokens > state.request_input_budget:
                recorder = current_model_audit()
                if recorder is not None:
                    recorder.append(
                        "steer_checkpoint",
                        model_visible=False,
                        status="refused",
                        payload={
                            "applied_revision": state.applied_revision,
                            "reason": "request_input_budget_exceeded",
                            "estimated_tokens": tokens,
                            "input_budget": state.request_input_budget,
                        },
                    )
                    await recorder.flush()
                raise RuntimeError("Steered request exceeds the captured input budget")
        revision = state.application_revision
        response = await invoke(messages, *args, **kwargs)
        if not state.pending:
            return response
        if state.on_request is not None:
            # Companion overflow returns only unapplied sources. Keep this exact
            # candidate if no fitting correction changed the model transcript.
            await apply_model_steering(messages, format_messages=format_messages)
            if state.application_revision == revision:
                return response
        # Nothing from this candidate has crossed the external-tool boundary.
        discard_pending_native_images()
        recorder = current_model_audit()
        if recorder is not None:
            recorder.append(
                "steer_checkpoint",
                model_visible=False,
                payload={"request_revision": revision, "status": "candidate_replaced"},
            )
            await recorder.flush()
