"""Import-safe in-memory AgentEvent drafts for one chat turn."""

from __future__ import annotations

import json
import asyncio
from datetime import datetime, timedelta
from dataclasses import field, replace, dataclass
from collections.abc import Mapping, Callable, Sequence, Awaitable

from .types import JSONType
from .tool_trace import ToolTraceEvent, PendingToolCall
from .tool_trace_safety import sanitize_json


@dataclass(frozen=True, slots=True)
class AgentEventDraft:
    sequence: int
    attempt: int
    event_type: str
    role: str = ""
    tool_call_id: str = ""
    execution_ref: str = ""
    tool_name: str = ""
    payload: dict[str, JSONType] = field(default_factory=dict)
    status: str = ""
    effect: str = ""
    duration_ms: int = 0
    model_visible: bool = True
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class AgentTurnRecorder:
    """Collect ordered drafts with an optional generation-local durable sink."""

    events: list[AgentEventDraft] = field(default_factory=list)
    _next_sequence: int = field(default=0, init=False)
    _attempt: int = field(default=0, init=False)
    _flushed: int = field(default=0, init=False)
    _inflight: int = field(default=0, init=False)
    sink: Callable[[Sequence[AgentEventDraft]], Awaitable[object]] | None = field(default=None, repr=False)
    warn: Callable[[str], object] | None = field(default=None, repr=False)
    _flush_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _tool_starts: set[str] = field(default_factory=set, init=False)
    _tool_results: set[str] = field(default_factory=set, init=False)

    async def flush(self) -> bool:
        """Serialize incremental commits; sink errors never replay tool side effects."""
        if self.sink is None:
            return True
        async with self._flush_lock:
            pending = self.pending_events()
            if not pending:
                return True
            self._inflight = len(pending)
            task = asyncio.ensure_future(self.sink(pending))
            cancelled = False
            try:
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    cancelled = True
                    await task
                self.mark_flushed(len(pending))
            except Exception as exc:
                if self.warn is not None:
                    self.warn(f"agent event persistence failed: {type(exc).__name__}")
                if cancelled:
                    raise asyncio.CancelledError from exc
                return False
            finally:
                self._inflight = 0
            if cancelled:
                raise asyncio.CancelledError
            return True

    @property
    def attempt(self) -> int:
        return self._attempt

    def next_attempt(self) -> int:
        self._attempt += 1
        return self._attempt

    def append(
        self,
        event_type: str,
        *,
        attempt: int | None = None,
        role: str = "",
        tool_call_id: str = "",
        execution_ref: str = "",
        tool_name: str = "",
        payload: Mapping[str, object] | None = None,
        status: str = "",
        effect: str = "",
        duration_ms: int = 0,
        model_visible: bool = True,
        created_at: datetime | None = None,
    ) -> AgentEventDraft:
        raw_payload = dict(payload or {})
        try:
            normalized_payload = json.loads(json.dumps(raw_payload, ensure_ascii=False))
        except (TypeError, ValueError):
            sanitized = sanitize_json(raw_payload, max_text=50_000)
            normalized_payload = sanitized if isinstance(sanitized, dict) else {}
        self._next_sequence += 1
        event = AgentEventDraft(
            sequence=self._next_sequence,
            attempt=self._attempt if attempt is None else max(0, attempt),
            event_type=event_type,
            role=role,
            tool_call_id=tool_call_id,
            execution_ref=execution_ref,
            tool_name=tool_name,
            payload=normalized_payload,
            status=status,
            effect=effect,
            duration_ms=max(0, int(duration_ms)),
            model_visible=model_visible,
            created_at=created_at or datetime.utcnow(),
        )
        self.events.append(event)
        return event

    def record_user_input(
        self,
        content: str,
        *,
        user_name: str,
        fresh_context: bool,
        attachments: Sequence[Mapping[str, object]] = (),
    ) -> None:
        payload: dict[str, object] = {
            "content": content,
            "speaker": user_name,
            "fresh_context": fresh_context,
        }
        if attachments:
            payload["attachments"] = list(attachments)
        self.append("user_input", role="user", payload=payload)

    def record_persona_state(self, payload: Mapping[str, object]) -> AgentEventDraft:
        """Record the persona, relationship, and memory inputs that shaped this turn."""

        return self.append("persona_state", payload=payload, model_visible=False)

    def record_model_attempt(
        self,
        *,
        attempt: int,
        model_name: str,
        status: str,
        duration_ms: int,
        content: str = "",
        error: str = "",
        metrics: object = None,
        model_visible: bool = False,
    ) -> None:
        payload: dict[str, object] = {"model": model_name}
        if content:
            payload["content"] = content
        if error:
            payload["error"] = error
        if metrics is not None:
            payload["metrics"] = metrics
        self.append(
            "model_attempt",
            attempt=attempt,
            role="assistant",
            payload=payload,
            status=status,
            duration_ms=duration_ms,
            model_visible=model_visible,
        )

    def record_tool_start(self, event: PendingToolCall | ToolTraceEvent) -> None:
        if event.execution_ref in self._tool_starts:
            return
        self._tool_starts.add(event.execution_ref)
        self.append(
            "assistant_tool_call",
            attempt=event.attempt,
            role="assistant",
            tool_call_id=event.tool_call_id or event.execution_ref,
            execution_ref=event.execution_ref,
            tool_name=event.tool_name,
            payload={
                "arguments": event.recorded_arguments,
                "context_arguments": event.arguments,
                "audit_arguments": event.audit_arguments,
                "provider_tool_call_id": event.tool_call_id,
            },
            status="requested",
            effect="none",
            created_at=self._tool_started_at(event),
        )

    def record_tool_events(self, events: Sequence[ToolTraceEvent]) -> None:
        for event in sorted(events, key=lambda item: item.sequence):
            self.record_tool_start(event)
            if event.execution_ref in self._tool_results:
                continue
            self._tool_results.add(event.execution_ref)
            result_payload: dict[str, object] = {
                "result": event.recorded_result,
                "context_result": event.outcome,
                "audit_result": event.audit_result,
            }
            if event.evidence:
                result_payload["evidence"] = event.evidence
            self.append(
                "tool_result",
                attempt=event.attempt,
                role="tool",
                tool_call_id=event.tool_call_id or event.execution_ref,
                execution_ref=event.execution_ref,
                tool_name=event.tool_name,
                payload=result_payload,
                status=event.status,
                effect=event.effect,
                duration_ms=event.duration_ms,
                created_at=self._tool_started_at(event) + timedelta(milliseconds=event.duration_ms),
            )
        self._resequence_chronologically()

    def pending_events(self) -> tuple[AgentEventDraft, ...]:
        """Return events recorded since the last flush watermark."""

        return tuple(self.events[self._flushed :])

    def mark_flushed(self, count: int) -> None:
        """Freeze sequences for events already written to durable storage."""

        self._flushed = min(len(self.events), max(self._flushed, self._flushed + max(0, count)))

    def _resequence_chronologically(self) -> None:
        boundary = self._flushed + self._inflight
        frozen = self.events[:boundary]
        ordered = sorted(self.events[boundary:], key=lambda event: (event.created_at, event.sequence))
        renumbered = [replace(event, sequence=index) for index, event in enumerate(ordered, start=boundary + 1)]
        self.events = frozen + renumbered
        self._next_sequence = len(self.events)

    def _tool_started_at(self, event: PendingToolCall | ToolTraceEvent) -> datetime:
        """Keep user input and immutable commits first, not later model records."""
        started_at = event.started_at.replace(tzinfo=None)
        boundary = self._flushed + self._inflight
        floor = max(
            (
                item.created_at
                for index, item in enumerate(self.events)
                if index < boundary or item.event_type == "user_input"
            ),
            default=started_at,
        )
        return max(started_at, floor)

    def record_assistant_output(self, content: str, *, status: str = "confirmed") -> None:
        if content:
            self.append(
                "assistant_output",
                role="assistant",
                payload={"content": content},
                status=status,
                effect="confirmed" if status == "confirmed" else "none",
            )
