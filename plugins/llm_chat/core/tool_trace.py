"""Generation-local tool execution trace collection."""

from __future__ import annotations

import time
from typing import cast
from secrets import token_hex
from datetime import datetime, timezone
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import field, dataclass
from collections.abc import Mapping, Iterator

from .types import JSONType
from .errors import summarize_exception
from .tool_trace_policy import (
    ToolEffect,
    ToolStatus,
    DeliverySnapshot,
    tool_error_effect,
    classify_tool_error,
    project_tool_success,
    project_tool_arguments,
)
from .tool_record_policy import record_tool_result, record_tool_arguments


@dataclass(frozen=True, slots=True)
class PendingToolCall:
    """One started call awaiting a terminal trace event."""

    sequence: int
    attempt: int
    execution_ref: str
    tool_name: str
    arguments: dict[str, JSONType]
    recorded_arguments: dict[str, JSONType]
    started_at: datetime
    started_monotonic: float


@dataclass(frozen=True, slots=True)
class ToolTraceEvent:
    """One durable tool execution plus its compact context projection."""

    sequence: int
    attempt: int
    execution_ref: str
    tool_name: str
    status: ToolStatus
    effect: ToolEffect
    arguments: dict[str, JSONType]
    outcome: dict[str, JSONType]
    recorded_arguments: dict[str, JSONType]
    recorded_result: JSONType
    evidence: dict[str, JSONType]
    started_at: datetime
    duration_ms: int


@dataclass
class ToolTraceRecorder:
    """Collect tool events for one generation without performing I/O."""

    events: list[ToolTraceEvent] = field(default_factory=list)
    attempt: int = 1
    _next_sequence: int = field(default=0, init=False)
    _evidence: dict[str, dict[str, JSONType]] = field(default_factory=dict, init=False)

    def set_attempt(self, attempt: int) -> None:
        self.attempt = max(1, int(attempt))

    def start(self, tool_name: str, arguments: Mapping[str, object]) -> PendingToolCall:
        self._next_sequence += 1
        return PendingToolCall(
            sequence=self._next_sequence,
            attempt=self.attempt,
            execution_ref=f"exec_{token_hex(10)}",
            tool_name=tool_name,
            arguments=project_tool_arguments(tool_name, arguments),
            recorded_arguments=record_tool_arguments(tool_name, arguments),
            started_at=datetime.now(timezone.utc),
            started_monotonic=time.monotonic(),
        )

    def record_evidence(self, execution_ref: str, payload: Mapping[str, object]) -> None:
        """Merge one tool's own confirmed delivery evidence for later audit."""

        if not execution_ref:
            return
        current = self._evidence.setdefault(execution_ref, {})
        for key, value in payload.items():
            if isinstance(value, list):
                existing = current.get(key)
                merged = list(existing) if isinstance(existing, list) else []
                merged.extend(cast(list[JSONType], value))
                current[key] = cast(JSONType, merged)
                continue
            current[key] = cast(JSONType, value)

    def finish_success(
        self,
        call: PendingToolCall,
        result: object,
        *,
        before: DeliverySnapshot,
        after: DeliverySnapshot,
    ) -> None:
        status, effect, outcome = project_tool_success(call.tool_name, result, before=before, after=after)
        self._append(
            call,
            status=status,
            effect=effect,
            outcome=outcome,
            recorded_result=record_tool_result(call.tool_name, result, projected_result=outcome),
        )

    def finish_error(
        self,
        call: PendingToolCall,
        exc: BaseException,
        *,
        before: DeliverySnapshot,
        after: DeliverySnapshot,
    ) -> None:
        status, error_code = classify_tool_error(exc, delivery_attempted=after.attempts > before.attempts)
        error: dict[str, JSONType] = {"error_code": error_code, "error": summarize_exception(exc)}
        self._append(
            call,
            status=status,
            effect=tool_error_effect(call.tool_name, before, after, terminal_status=status),
            outcome=error,
            recorded_result=error,
        )

    def finish_cancelled(
        self,
        call: PendingToolCall,
        *,
        before: DeliverySnapshot,
        after: DeliverySnapshot,
    ) -> None:
        error: dict[str, JSONType] = {"error_code": "cancelled", "error": "Tool execution was cancelled"}
        self._append(
            call,
            status="cancelled",
            effect=tool_error_effect(call.tool_name, before, after, terminal_status="cancelled"),
            outcome=error,
            recorded_result=error,
        )

    def _append(
        self,
        call: PendingToolCall,
        *,
        status: ToolStatus,
        effect: ToolEffect,
        outcome: dict[str, JSONType],
        recorded_result: JSONType,
    ) -> None:
        duration_ms = max(0, round((time.monotonic() - call.started_monotonic) * 1000))
        evidence = self._evidence.pop(call.execution_ref, {})
        if (
            status in {"failed", "cancelled"}
            and call.tool_name in {"publish_web_preview", "revoke_web_preview"}
            and evidence.get("artifact_effect") in ("published", "revoked")
        ):
            effect = "partial"
        self.events.append(
            ToolTraceEvent(
                sequence=call.sequence,
                attempt=call.attempt,
                execution_ref=call.execution_ref,
                tool_name=call.tool_name,
                status=status,
                effect=effect,
                arguments=call.arguments,
                outcome=outcome,
                recorded_arguments=call.recorded_arguments,
                recorded_result=recorded_result,
                evidence=evidence,
                started_at=call.started_at,
                duration_ms=duration_ms,
            )
        )


_ACTIVE_TOOL_TRACE: ContextVar[ToolTraceRecorder | None] = ContextVar(
    "llm_chat_tool_trace",
    default=None,
)
_ACTIVE_EXECUTION_REF: ContextVar[str] = ContextVar("llm_chat_tool_execution_ref", default="")


@contextmanager
def llm_chat_tool_trace_scope(recorder: ToolTraceRecorder) -> Iterator[None]:
    """Expose one mutable recorder to every tool task in the generation."""

    token = _ACTIVE_TOOL_TRACE.set(recorder)
    try:
        yield
    finally:
        _ACTIVE_TOOL_TRACE.reset(token)


def current_tool_trace() -> ToolTraceRecorder | None:
    """Return the active generation recorder, if any."""

    return _ACTIVE_TOOL_TRACE.get()


def current_tool_execution_ref() -> str:
    """Return the active tool execution reference, if any."""

    return _ACTIVE_EXECUTION_REF.get()


@contextmanager
def llm_chat_tool_execution_scope(execution_ref: str) -> Iterator[None]:
    """Bind one in-flight tool call so its handler can attach delivery evidence."""

    token = _ACTIVE_EXECUTION_REF.set(execution_ref)
    try:
        yield
    finally:
        _ACTIVE_EXECUTION_REF.reset(token)


def record_tool_evidence(payload: Mapping[str, object]) -> None:
    """Record confirmed delivery evidence for the innermost active tool call."""

    recorder = _ACTIVE_TOOL_TRACE.get()
    execution_ref = _ACTIVE_EXECUTION_REF.get()
    if recorder is None or not execution_ref:
        return
    recorder.record_evidence(execution_ref, payload)
