"""Task-local cooperative input steering without canceling the owner generation."""

from __future__ import annotations

from typing import TYPE_CHECKING
import asyncio
from datetime import UTC, datetime
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from collections.abc import Callable, Iterator, Awaitable

from arclet.entari import Session
from arclet.letoderea.context import Contexts

from .core.types import ChatMessage
from .core.delivery import DeliveryRejected

if TYPE_CHECKING:
    from .agent_context import AgentAccessContext

ParticipantScope = tuple[str, str, str, str]
MAX_STEER_INPUTS = 64


def participant_scope(session: Session) -> ParticipantScope:
    return (
        str(session.account.platform),
        str(session.account.self_id),
        str(session.channel.id),
        str(session.user.id),
    )


def _message_id(session: Session) -> str | None:
    message = getattr(getattr(session, "event", None), "message", None)
    message_id = getattr(message, "id", None)
    return str(message_id) if message_id is not None and str(message_id) else None


@dataclass(frozen=True, slots=True)
class SteerInput:
    sequence: int
    session: Session
    ctx: Contexts
    received_at: datetime


class SteerPending(DeliveryRejected):
    """New input must be applied before an unattempted external effect."""


class SteeringState:
    """A single shared mutable object inherited by every generation child task."""

    def __init__(self, root_session: Session, root_ctx: Contexts) -> None:
        self.scope = participant_scope(root_session)
        self.revision = 1
        self.applied_revision = 1
        self.accepting = True
        self.closed = False
        self.processor: Callable[[SteerInput], Awaitable[ChatMessage | None]] | None = None
        self.on_request: Callable[[list[ChatMessage]], None] | None = None
        self.request_budget_snapshot: tuple[frozenset[int], list[ChatMessage]] | None = None
        self.on_receive: Callable[[SteerInput], None] | None = None
        self.on_flush: Callable[[], Awaitable[object]] | None = None
        self.on_close: Callable[[], Awaitable[object]] | None = None
        self.access: AgentAccessContext | None = None
        root = SteerInput(1, root_session, root_ctx, datetime.now(UTC).replace(tzinfo=None))
        self.request_input_budget = 0
        self.request_model_name: str | None = None
        self.media_requested = False
        self.media_request_timeout: float | None = None
        self.media_confirmation_baseline = 0
        self._inputs = [root]
        self._events = [root_session.event]
        root_id = _message_id(root_session)
        self._message_ids = {root_id} if root_id is not None else set()
        self._drain_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._close_complete = False
        self._processing_error: BaseException | None = None

    @property
    def inputs(self) -> tuple[SteerInput, ...]:
        return tuple(self._inputs)

    @property
    def pending(self) -> bool:
        return self.applied_revision < self.revision

    @property
    def application_revision(self) -> int:
        return self.applied_revision

    def input_applied(self, item: SteerInput) -> bool:
        return item.sequence <= self.applied_revision

    def input_deferred(self, item: SteerInput) -> bool:
        return False

    def accepts_input(self, session: Session) -> bool:
        """Keep ordinary steering member-local; scoped dialogue may override admission."""
        return participant_scope(session) == self.scope

    def owns(self, session: Session) -> bool:
        return self.accepts_input(session) and any(session.event is event for event in self._events)

    def defer_input(self, session: Session, *, budget_exceeded: bool = False) -> bool:
        """Ordinary participant steering refuses overflow instead of transferring ownership."""
        return False

    def receive(self, session: Session, ctx: Contexts, *, received_at: datetime | None = None) -> bool:
        """Atomically admit an input, or acknowledge a real protocol-ID duplicate."""
        if not self.accepts_input(session):
            return False
        message_id = _message_id(session)
        if self.owns(session) or (message_id is not None and message_id in self._message_ids):
            return True
        if self.closed or not self.accepting or len(self._inputs) >= MAX_STEER_INPUTS:
            return False
        entry = SteerInput(self.revision + 1, session, ctx, received_at or datetime.now(UTC).replace(tzinfo=None))
        self._inputs.append(entry)
        self._events.append(session.event)
        if message_id is not None:
            self._message_ids.add(message_id)
        self.revision = entry.sequence
        if self.on_receive is not None:
            self.on_receive(entry)
        return True

    async def flush(self) -> None:
        if self.on_flush is not None and await self.on_flush() is False:
            raise RuntimeError("Participant input receipt could not be persisted")

    async def drain(self) -> list[ChatMessage]:
        """Apply every pending input once in arrival order, including arrivals while awaiting."""
        async with self._drain_lock:
            self._check_open()
            if self._processing_error is not None:
                raise RuntimeError("Participant steering input processing failed") from self._processing_error
            messages: list[ChatMessage] = []
            while self.pending:
                if self.processor is None:
                    raise RuntimeError("Steering inputs have not been bound")
                entry = self._inputs[self.applied_revision]
                await self.flush()
                self._check_open()
                try:
                    message = await self.processor(entry)
                except BaseException as exc:
                    self._processing_error = exc
                    raise
                self._check_open()
                if message is None:
                    raise RuntimeError("Deferred input requires a dialogue owner")
                self.applied_revision = entry.sequence
                messages.append(message)
            return messages

    def seal(self) -> bool:
        """Close admission only when the last received revision has been applied."""
        if self.closed or self.pending:
            return False
        self.accepting = False
        return True

    def _check_open(self) -> None:
        if self.closed:
            raise asyncio.CancelledError("Participant steering generation has closed")

    async def aclose(self) -> None:
        """Invalidate inherited child scopes and preserve unapplied receipts in audit."""
        self.closed = True
        self.accepting = False
        async with self._close_lock:
            if self._close_complete:
                return
            async with self._drain_lock:
                if self.on_close is not None:
                    await self.on_close()
                await self.flush()
                self._close_complete = True


_CURRENT_STEERING: ContextVar[SteeringState | None] = ContextVar("llm_chat_steering", default=None)


def current_steering() -> SteeringState | None:
    return _CURRENT_STEERING.get()


@contextmanager
def steering_scope(state: SteeringState) -> Iterator[SteeringState]:
    token = _CURRENT_STEERING.set(state)
    try:
        yield state
    finally:
        _CURRENT_STEERING.reset(token)


def check_steer_boundary() -> None:
    state = current_steering()
    if state is None:
        return
    state._check_open()
    if not state.accepting:
        raise asyncio.CancelledError("Participant steering generation has settled")
    if state.pending:
        raise SteerPending("New participant input is awaiting application")
