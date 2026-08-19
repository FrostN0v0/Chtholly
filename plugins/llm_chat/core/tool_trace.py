"""Generation-local tool execution trace collection."""

from __future__ import annotations

import time
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


@dataclass(frozen=True, slots=True)
class PendingToolCall:
    """One started call awaiting a terminal trace event."""

    sequence: int
    tool_name: str
    arguments: dict[str, JSONType]
    started_at: datetime
    started_monotonic: float


@dataclass(frozen=True, slots=True)
class ToolTraceEvent:
    """A bounded, persistence-safe tool execution record."""

    sequence: int
    tool_name: str
    status: ToolStatus
    effect: ToolEffect
    arguments: dict[str, JSONType]
    outcome: dict[str, JSONType]
    started_at: datetime
    duration_ms: int


@dataclass
class ToolTraceRecorder:
    """Collect tool events for one generation without performing I/O."""

    events: list[ToolTraceEvent] = field(default_factory=list)
    _next_sequence: int = field(default=0, init=False)

    def start(self, tool_name: str, arguments: Mapping[str, object]) -> PendingToolCall:
        self._next_sequence += 1
        return PendingToolCall(
            sequence=self._next_sequence,
            tool_name=tool_name,
            arguments=project_tool_arguments(tool_name, arguments),
            started_at=datetime.now(timezone.utc),
            started_monotonic=time.monotonic(),
        )

    def finish_success(
        self,
        call: PendingToolCall,
        result: object,
        *,
        before: DeliverySnapshot,
        after: DeliverySnapshot,
    ) -> None:
        status, effect, outcome = project_tool_success(call.tool_name, result, before=before, after=after)
        self._append(call, status=status, effect=effect, outcome=outcome)

    def finish_error(
        self,
        call: PendingToolCall,
        exc: BaseException,
        *,
        before: DeliverySnapshot,
        after: DeliverySnapshot,
    ) -> None:
        status, error_code = classify_tool_error(exc, delivery_attempted=after.attempts > before.attempts)
        self._append(
            call,
            status=status,
            effect=tool_error_effect(call.tool_name, before, after, terminal_status=status),
            outcome={"error_code": error_code, "error": summarize_exception(exc)},
        )

    def finish_cancelled(
        self,
        call: PendingToolCall,
        *,
        before: DeliverySnapshot,
        after: DeliverySnapshot,
    ) -> None:
        self._append(
            call,
            status="cancelled",
            effect=tool_error_effect(call.tool_name, before, after, terminal_status="cancelled"),
            outcome={"error_code": "cancelled", "error": "Tool execution was cancelled"},
        )

    def _append(
        self,
        call: PendingToolCall,
        *,
        status: ToolStatus,
        effect: ToolEffect,
        outcome: dict[str, JSONType],
    ) -> None:
        duration_ms = max(0, round((time.monotonic() - call.started_monotonic) * 1000))
        self.events.append(
            ToolTraceEvent(
                sequence=call.sequence,
                tool_name=call.tool_name,
                status=status,
                effect=effect,
                arguments=call.arguments,
                outcome=outcome,
                started_at=call.started_at,
                duration_ms=duration_ms,
            )
        )


_ACTIVE_TOOL_TRACE: ContextVar[ToolTraceRecorder | None] = ContextVar(
    "llm_chat_tool_trace",
    default=None,
)


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
