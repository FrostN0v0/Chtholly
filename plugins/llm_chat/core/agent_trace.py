"""Import-safe in-memory AgentEvent drafts for one chat turn."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from dataclasses import field, replace, dataclass
from collections.abc import Mapping, Sequence

from .types import JSONType
from .tool_trace import ToolTraceEvent
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
    """Collect one turn's durable events before a single transactional flush."""

    events: list[AgentEventDraft] = field(default_factory=list)
    _next_sequence: int = field(default=0, init=False)
    _attempt: int = field(default=0, init=False)
    _flushed: int = field(default=0, init=False)

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

    def record_tool_events(self, events: Sequence[ToolTraceEvent]) -> None:
        floor = self._earliest_tool_time()
        for event in sorted(events, key=lambda item: item.sequence):
            started_at = event.started_at.replace(tzinfo=None)
            if floor is not None and started_at < floor:
                started_at = floor
            self.append(
                "assistant_tool_call",
                attempt=event.attempt,
                role="assistant",
                tool_call_id=event.execution_ref,
                execution_ref=event.execution_ref,
                tool_name=event.tool_name,
                payload={
                    "arguments": event.recorded_arguments,
                    "context_arguments": event.arguments,
                },
                status="requested",
                effect="none",
                model_visible=True,
                created_at=started_at,
            )
            result_payload: dict[str, object] = {
                "result": event.recorded_result,
                "context_result": event.outcome,
            }
            if event.evidence:
                result_payload["evidence"] = event.evidence
            self.append(
                "tool_result",
                attempt=event.attempt,
                role="tool",
                tool_call_id=event.execution_ref,
                execution_ref=event.execution_ref,
                tool_name=event.tool_name,
                payload=result_payload,
                status=event.status,
                effect=event.effect,
                duration_ms=event.duration_ms,
                model_visible=True,
                created_at=started_at + timedelta(milliseconds=event.duration_ms),
            )
        self._resequence_chronologically()

    def pending_events(self) -> tuple[AgentEventDraft, ...]:
        """Return events recorded since the last flush watermark."""

        return tuple(self.events[self._flushed :])

    def mark_flushed(self, count: int) -> None:
        """Freeze sequences for events already written to durable storage."""

        self._flushed = min(len(self.events), max(self._flushed, self._flushed + max(0, count)))

    def _resequence_chronologically(self) -> None:
        frozen = self.events[: self._flushed]
        ordered = sorted(self.events[self._flushed :], key=lambda event: (event.created_at, event.sequence))
        renumbered = [replace(event, sequence=index) for index, event in enumerate(ordered, start=self._flushed + 1)]
        self.events = frozen + renumbered
        self._next_sequence = len(self.events)

    def _earliest_tool_time(self) -> datetime | None:
        """Return the floor that keeps tool events after already-recorded turn events."""

        return max((event.created_at for event in self.events), default=None)

    def record_assistant_output(self, content: str, *, status: str = "confirmed") -> None:
        if content:
            self.append(
                "assistant_output",
                role="assistant",
                payload={"content": content},
                status=status,
                effect="confirmed" if status == "confirmed" else "none",
            )
