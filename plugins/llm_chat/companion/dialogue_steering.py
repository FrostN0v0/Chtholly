"""Channel-local cooperative steering used only by enabled companion dialogue."""

from __future__ import annotations

from typing import TYPE_CHECKING
import asyncio
from collections.abc import Callable, Sequence

from arclet.entari import Session

from ..steering import SteerInput, SteeringState, participant_scope
from ..core.types import ChatMessage

if TYPE_CHECKING:
    from .dialogue_manager import DialogueInput
    from ..agent_turn_setup import PreparedAgentTurn


class DialogueSteeringState(SteeringState):
    def __init__(
        self,
        root: DialogueInput,
        initial: Sequence[DialogueInput],
        *,
        defer_input: Callable[[Session], bool] | None = None,
    ) -> None:
        super().__init__(root.session, root.ctx)
        self._inputs[0] = SteerInput(1, root.session, root.ctx, root.received_at)
        self._order = {id(item.session.event): item.sequence for item in initial}
        self._order.setdefault(id(root.session.event), -1)
        self._directed = {id(item.session.event): item.directed for item in initial}
        self._directed.setdefault(id(root.session.event), root.directed)
        self.prepared: PreparedAgentTurn | None = None
        self.status = "failed"
        self.initial_applied = False
        self._withdrawn = False
        self._return_input = defer_input
        self._budget_cutoff: int | None = None
        self._deferred_events: set[int] = set()
        self._resolved_sequences = {1}
        self._applied_order = [1]
        self.on_request = self._capture_request

    def _capture_request(self, messages: list[ChatMessage]) -> None:
        self.request_budget_snapshot = (frozenset(self._applied_order), messages)

    @property
    def pending(self) -> bool:
        return len(self._resolved_sequences) < len(self.inputs)

    @property
    def application_revision(self) -> int:
        return len(self._applied_order)

    def input_applied(self, item: SteerInput) -> bool:
        return item.sequence in self._applied_order

    def input_deferred(self, item: SteerInput) -> bool:
        return id(item.session.event) in self._deferred_events

    def defer_unprepared_inputs(self) -> None:
        """Return untouched sources if root preparation never reached a model request.

        The failed root is consumed once, including when it alone is oversized.
        No supplement may have entered a model or tool transcript at this point.
        """
        if self.prepared is not None:
            return
        for item in self.inputs[1:]:
            if not self.input_applied(item):
                self.defer_input(item.session, budget_exceeded=True)

    async def drain(self) -> list[ChatMessage]:
        """Prefer directed corrections without crediting returned sources as applied."""
        async with self._drain_lock:
            self._check_open()
            if self._processing_error is not None:
                raise RuntimeError("Participant steering input processing failed") from self._processing_error
            messages: list[ChatMessage] = []
            while self.pending:
                if self.processor is None:
                    raise RuntimeError("Steering inputs have not been bound")
                await self.flush()
                self._check_open()
                unresolved = [item for item in self.inputs if item.sequence not in self._resolved_sequences]
                entry = min(unresolved, key=lambda item: (not self.directed(item.session), item.sequence))
                try:
                    message = await self.processor(entry)
                except BaseException as exc:
                    self._processing_error = exc
                    raise
                self._check_open()
                self._resolved_sequences.add(entry.sequence)
                if message is not None:
                    self._applied_order.append(entry.sequence)
                    self.applied_revision = max(self.applied_revision, entry.sequence)
                    messages.append(message)
            return messages

    def owns(self, session: Session) -> bool:
        return id(session.event) not in self._deferred_events and super().owns(session)

    def defer_input(self, session: Session, *, budget_exceeded: bool = False) -> bool:
        source_order = self._order.get(id(session.event))
        ordered_after_overflow = (
            self._budget_cutoff is not None and source_order is not None and source_order >= self._budget_cutoff
        )
        if (
            self.closed
            or self._return_input is None
            or not self.owns(session)
            or session.event is self.inputs[0].session.event
            or (not budget_exceeded and (not ordered_after_overflow or self.directed(session)))
        ):
            return False
        if not self._return_input(session):
            return False
        if source_order is not None:
            self._budget_cutoff = (
                source_order if self._budget_cutoff is None else min(self._budget_cutoff, source_order)
            )
        self._deferred_events.add(id(session.event))
        return True

    def invalidate_source(self, message_id: str) -> bool:
        if any(
            str(getattr(getattr(item.session.event, "message", None), "id", "") or "") == message_id
            for item in self.inputs
            if self.owns(item.session)
        ):
            self._withdrawn = True
            self.accepting = False
            return True
        return False

    def _check_open(self) -> None:
        super()._check_open()
        if self._withdrawn:
            raise asyncio.CancelledError("Companion input was edited or withdrawn")

    def accepts_input(self, session: Session) -> bool:
        return participant_scope(session)[:3] == self.scope[:3]

    def directed(self, session: Session) -> bool:
        return self._directed.get(id(session.event), False)

    def admit(self, item: DialogueInput) -> bool:
        if self._budget_cutoff is not None and not item.directed:
            return False
        event_id = id(item.session.event)
        existing = event_id in self._order
        self._order.setdefault(event_id, item.sequence)
        self._directed[event_id] = item.directed
        if not self.receive(item.session, item.ctx, received_at=item.received_at):
            if not existing:
                self._order.pop(event_id)
                self._directed.pop(event_id)
            return False
        return True

    async def prepare_first_request(self, prepared: PreparedAgentTurn) -> None:
        """Retain actual arrival order even when a directed input owns tool authority."""
        if self.initial_applied:
            raise RuntimeError("Companion initial dialogue was already applied")
        self.prepared = prepared
        from ..steer_inputs import SteerInputRejected
        from ..context_builder import estimate_tokens

        root_messages = [{"role": "system", "content": prepared.system}, *prepared.chat_messages]
        if (
            self.request_input_budget > 0
            and estimate_tokens(self.request_model_name, root_messages) > self.request_input_budget
        ):
            # The root has no smaller predecessor to fall back to. Consume its
            # refusal once; only subsequent real sources can move to the next owner.
            await self.drain()
            prepared.agent_events.append(
                "steer_checkpoint",
                model_visible=False,
                status="refused",
                effect="none",
                payload={"steer_sequence": 1, "reason": "input_budget_exceeded"},
            )
            await prepared.agent_events.flush()
            raise SteerInputRejected("input_budget_exceeded")
        additions = await self.drain()
        if additions:
            root = prepared.chat_messages[-1]
            if root.get("role") != "user":
                raise RuntimeError("Companion dialogue has no current user input")
            entries = [self.inputs[sequence - 1] for sequence in self._applied_order]
            ordered = sorted(
                zip(entries, [root, *additions], strict=True),
                key=lambda pair: self._order[id(pair[0].session.event)],
            )
            prepared.chat_messages[-1:] = [message for _, message in ordered]
        self.request_budget_snapshot = None
        self.initial_applied = True
