"""Generation-scoped capture at the local LiteLLM and HTTP request boundaries."""

from __future__ import annotations

import json
import time
from typing import Any
import asyncio
from secrets import token_hex
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import field, dataclass
from collections.abc import Mapping, Callable, Iterator, Awaitable

from .core.agent_trace import AgentTurnRecorder
from .core.model_audit import sanitize_model_request, sanitize_model_response

_ACTIVE_RECORDER: ContextVar[AgentTurnRecorder | None] = ContextVar("llm_chat_model_audit", default=None)


@dataclass
class _Completion:
    recorder: AgentTurnRecorder
    parameters: dict[str, Any]
    attempt: int
    group_id: str = field(default_factory=lambda: f"completion_{token_hex(10)}")
    requests: int = 0


_ACTIVE_COMPLETION: ContextVar[_Completion | None] = ContextVar("llm_chat_completion_audit", default=None)


def current_model_audit() -> AgentTurnRecorder | None:
    return _ACTIVE_RECORDER.get()


@contextmanager
def model_audit_scope(recorder: AgentTurnRecorder | None) -> Iterator[None]:
    """Limit capture to explicitly opted-in main generation calls."""
    token = _ACTIVE_RECORDER.set(recorder)
    completion_token = _ACTIVE_COMPLETION.set(None)
    try:
        yield
    finally:
        _ACTIVE_COMPLETION.reset(completion_token)
        _ACTIVE_RECORDER.reset(token)


def _append(
    state: _Completion, event_type: str, payload: dict[str, object], *, status: str, duration_ms: int = 0
) -> None:
    try:
        state.recorder.append(
            event_type,
            attempt=state.attempt,
            role="assistant",
            payload=payload,
            status=status,
            duration_ms=duration_ms,
            model_visible=False,
        )
    except Exception as exc:
        if state.recorder.warn is not None:
            state.recorder.warn(f"model audit capture failed: {type(exc).__name__}")


def _request(state: _Completion, parameters: Mapping[str, object], *, boundary: str) -> tuple[str, float]:
    request_id = f"request_{token_hex(12)}"
    state.requests += 1
    try:
        payload = sanitize_model_request(
            parameters.get("messages", parameters.get("input", parameters.get("contents", []))),
            tools=parameters.get("tools", ()),
            parameters={
                key: value
                for key, value in parameters.items()
                if key not in {"messages", "tools", "model", "input", "contents"}
            },
            model=str(parameters.get("model", state.parameters.get("model", ""))),
        )
    except Exception:
        payload = {
            "model": "",
            "messages": [],
            "tools": [],
            "parameters": {},
            "capture_status": "failed",
            "redactions": [{"path": "$", "type": "serialization_failed"}],
        }
    payload.update(request_id=request_id, completion_id=state.group_id, capture_boundary=boundary)
    _append(state, "model_request", payload, status="requested")
    return request_id, time.monotonic()


def _response(
    state: _Completion,
    request_id: str,
    started: float,
    response: object = None,
    *,
    status: str,
    error: BaseException | None = None,
    http_status: int | None = None,
) -> None:
    try:
        payload = sanitize_model_response(response, model=str(state.parameters.get("model", "")), error=error)
    except Exception:
        payload = {
            "model": "",
            "content": None,
            "tool_calls": [],
            "finish_reason": None,
            "usage": dict.fromkeys(
                ("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens", "reasoning_tokens")
            ),
            "capture_status": "failed",
            "redactions": [{"path": "$", "type": "serialization_failed"}],
        }
    payload.update(request_id=request_id, completion_id=state.group_id)
    if http_status is not None:
        payload["http_status"] = http_status
    _append(state, "model_response", payload, status=status, duration_ms=round((time.monotonic() - started) * 1000))


async def _flush_terminal(recorder: AgentTurnRecorder) -> None:
    # A cancelled generation must still settle the already-persisted request/tool.
    task = asyncio.create_task(recorder.flush())
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


async def capture_completion(call: Callable[..., Awaitable[Any]], **kwargs: Any) -> Any:
    """Capture only this SDK call; HTTP retries each receive their own request ID.

    Custom clients without HTTPX still produce a truthful SDK-boundary pair. They
    are explicitly labelled rather than claiming a network request was observed.
    """
    recorder = current_model_audit()
    if recorder is None or _ACTIVE_COMPLETION.get() is not None:
        return await call(**kwargs)
    state = _Completion(recorder, kwargs, recorder.attempt)
    token = _ACTIVE_COMPLETION.set(state)
    response = None
    failure: BaseException | None = None
    started = time.monotonic()
    try:
        response = await call(**kwargs)
        return response
    except BaseException as exc:
        failure = exc
        raise
    finally:
        _ACTIVE_COMPLETION.reset(token)
        if not state.requests:
            request_id, _ = _request(state, kwargs, boundary="sdk_call")
            _response(
                state,
                request_id,
                started,
                response,
                status="cancelled"
                if isinstance(failure, asyncio.CancelledError)
                else "failed"
                if failure
                else "succeeded",
                error=failure,
            )
        await _flush_terminal(recorder)


class AuditedLiteLLMClient:
    """Delegate everything unchanged except the opted-in async completion call."""

    def __init__(self, client: Any):
        self.client = client

    def __getattr__(self, name: str) -> Any:
        return getattr(self.client, name)

    async def acompletion(self, **kwargs: Any) -> Any:
        return await capture_completion(self.client.acompletion, **kwargs)


def install_model_http_audit() -> Callable[[], None]:
    """Install a context-aware HTTP bridge without changing third-party files.

    The context exists only while the local Agno client/finalizer awaits a chat
    completion. Tool, vision, evaluator and other plugin HTTP calls are ignored.
    HTTPX.send is invoked separately by provider SDK retries; its buffered body
    is observed without consuming streams or changing retry/timeout parameters.
    """
    import httpx

    previous = httpx.AsyncClient.send
    if getattr(previous, "__llm_chat_audit__", False):
        return lambda: None

    async def send(self: httpx.AsyncClient, request: httpx.Request, **kwargs: Any) -> httpx.Response:
        state = _ACTIVE_COMPLETION.get()
        if state is None or request.method != "POST":
            return await previous(self, request, **kwargs)
        try:
            body = request.content
            parameters = json.loads(body)
        except (ValueError, TypeError, httpx.RequestNotRead):
            parameters = None
        # Authentication refreshes, telemetry and unrelated HTTP are not models.
        if not isinstance(parameters, dict) or not any(key in parameters for key in ("messages", "input", "contents")):
            return await previous(self, request, **kwargs)
        request_id, started = _request(state, parameters, boundary="http")
        try:
            await state.recorder.flush()
            response = await previous(self, request, **kwargs)
        except BaseException as exc:
            _response(
                state,
                request_id,
                started,
                status="cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
                error=exc,
            )
            await _flush_terminal(state.recorder)
            raise
        try:
            response_body = response.json()
        except (ValueError, httpx.ResponseNotRead):
            response_body = None
        status = "failed" if response.status_code >= 400 else "succeeded"
        _response(state, request_id, started, response_body, status=status, http_status=response.status_code)
        await state.recorder.flush()
        return response

    setattr(send, "__llm_chat_audit__", True)
    httpx.AsyncClient.send = send

    def restore() -> None:
        if httpx.AsyncClient.send is send:
            httpx.AsyncClient.send = previous

    return restore
