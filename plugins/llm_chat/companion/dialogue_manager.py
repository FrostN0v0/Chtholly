"""One cooperative foreground owner per native companion scope, with finite alarms."""

from __future__ import annotations

from typing import Any, Literal
import asyncio
from datetime import UTC, datetime
from collections import deque
from dataclasses import field, dataclass
from collections.abc import Callable, Awaitable

from arclet.entari import Session
from arclet.entari.logger import log
from arclet.letoderea.context import Contexts

from utils.companion_core.dialogue import DialogueDecision, ambient_deadline

from . import dialogue_store
from .types import ModeSnapshot
from ..models import ChatScope
from .internal import settle_owned
from .dialogue_store import DialogueSchedule
from ..session_manager import scope_identity

_LOGGER = log.wrapper("[llm_chat.companion.dialogue]")


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class DialogueInput:
    session: Session
    ctx: Any
    directed: bool
    received_at: datetime
    sequence: int = 0
    characters: int = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "characters", len(self.session.elements.extract_plain_text()))
        if self.ctx is not None:
            # Native dispatch clears its context after the admitting listener returns.
            object.__setattr__(self, "ctx", Contexts(self.ctx.copy()))


@dataclass(frozen=True, slots=True)
class DialogueOutcome:
    decision: DialogueDecision = field(default_factory=DialogueDecision)
    status: Literal["completed", "unknown", "failed", "cancelled"] = "completed"
    confirmed_deliveries: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.decision, DialogueDecision):
            raise ValueError("A dialogue outcome requires a native dialogue decision")
        if self.status not in {"completed", "unknown", "failed", "cancelled"}:
            raise ValueError("Invalid dialogue outcome status")
        if (
            isinstance(self.confirmed_deliveries, bool)
            or not isinstance(self.confirmed_deliveries, int)
            or self.confirmed_deliveries < 0
        ):
            raise ValueError("Invalid confirmed dialogue delivery count")


@dataclass(slots=True)
class DialogueBatch:
    scope: ChatScope
    mode: ModeSnapshot
    inputs: tuple[DialogueInput, ...]
    opportunity: Any | None
    trigger: Literal["input", "wait", "opportunity"]
    session: Session
    ctx: Any
    _pull: Callable[[int | None], Awaitable[tuple[DialogueInput, ...]]] = field(repr=False)
    _valid: Callable[[], Awaitable[bool]] = field(repr=False)
    _bind: Callable[[Callable[[DialogueInput], bool]], None] = field(repr=False)
    _defer: Callable[[Session], bool] | None = field(default=None, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    on_invalidated: Callable[[str], bool] | None = field(default=None, init=False, repr=False)

    @property
    def account(self) -> Any:
        return self.session.account

    def bind_admitter(self, callback: Callable[[DialogueInput], bool]) -> None:
        """Bind a synchronous steering inbox before the runner's first external await.

        True transfers ownership exactly once. False leaves that input queued for
        the next generation; the callback must not perform transport or model IO.
        """
        if self._closed:
            raise RuntimeError("This dialogue batch has finished")
        self._bind(callback)

    async def admit_live(self, *, limit: int | None = None) -> tuple[DialogueInput, ...]:
        if self._closed:
            return ()
        return await self._pull(limit)

    async def valid(self) -> bool:
        return not self._closed and await self._valid()

    def defer_input(self, session: Session) -> bool:
        """Return one exactly owned, unapplied input after this generation settles."""
        return not self._closed and self._defer is not None and self._defer(session)


@dataclass(slots=True)
class _Opportunity:
    value: Any
    key: str | None
    session: Session | None
    ctx: Any


@dataclass(slots=True)
class _Owner:
    scope: ChatScope
    mode: ModeSnapshot
    schedule: DialogueSchedule | None
    pending: deque[DialogueInput] = field(default_factory=deque)
    seen_message_ids: set[str] = field(default_factory=set)
    message_order: deque[str] = field(default_factory=deque)
    space: asyncio.Event = field(default_factory=asyncio.Event)
    opportunity: _Opportunity | None = None
    session: Session | None = None
    ctx: Any = None
    task: asyncio.Task[None] | None = None
    predecessor: asyncio.Task[None] | None = None
    batch: DialogueBatch | None = None
    admitter: Callable[[DialogueInput], bool] | None = None
    admitted: dict[int, DialogueInput] = field(default_factory=dict)
    deferred: dict[int, DialogueInput] = field(default_factory=dict)
    executing: bool = False
    live_count: int = 0
    live_characters: int = 0
    opportunity_key: str | None = None
    used_opportunities: deque[str] = field(default_factory=lambda: deque(maxlen=128))
    stopped: bool = False
    transport_blocked: bool = False
    faulted: bool = False


class DialogueManager:
    def __init__(
        self,
        config: Any,
        runner: Callable[[DialogueBatch], Awaitable[DialogueOutcome]],
        *,
        clock: Callable[[], datetime] = _now,
        session_factory: Any = None,
        restore_session: Callable[[ChatScope], Awaitable[Session | None]] | None = None,
        max_pending_inputs: int = 128,
        max_batch_inputs: int = 16,
        max_batch_chars: int = 32000,
        max_live_inputs: int = 32,
    ) -> None:
        for name, value in (
            ("max_pending_inputs", max_pending_inputs),
            ("max_batch_inputs", max_batch_inputs),
            ("max_batch_chars", max_batch_chars),
            ("max_live_inputs", max_live_inputs),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"Invalid {name}")
        if not callable(runner):
            raise TypeError("A real dialogue runner is required")
        self.config = config
        self._runner = runner
        self._clock = clock
        self._factory = session_factory
        self._restore_session = restore_session
        self._max_pending = max_pending_inputs
        self._max_batch = max_batch_inputs
        self._max_chars = max_batch_chars
        self._max_live = max_live_inputs
        self._input_sequence = 0
        self._owners: dict[int, _Owner] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._wake = asyncio.Event()
        self._settled = asyncio.Event()
        self._start_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._owner_lock = asyncio.Lock()
        self._loop: asyncio.Task[None] | None = None
        self._started = False
        self._closed = False

    def wake(self) -> None:
        if not self._closed:
            self._settled.clear()
            for owner in self._owners.values():
                owner.transport_blocked = False
            self._wake.set()

    def active(self, scope_id: int) -> bool:
        owner = self._owners.get(scope_id)
        return (
            owner is not None
            and not owner.stopped
            and bool(owner.pending or owner.opportunity or (owner.task is not None and not owner.task.done()))
        )

    async def start(self) -> None:
        async with self._start_lock:
            if self._started or self._closed:
                return
            schedules = await dialogue_store.initialize_store(now=self._clock(), session_factory=self._factory)
            if self._closed:
                return
            for schedule in schedules:
                self._owners[schedule.scope.id] = _Owner(schedule.scope, schedule.mode, schedule)
            self._started = True
            self._loop = asyncio.create_task(self._run_loop(), name="companion-dialogue-wakeup")
            self.wake()

    async def _owner(self, scope: ChatScope, mode: ModeSnapshot) -> _Owner | None:
        async with self._owner_lock:
            predecessor = None
            while not self._closed:
                resolved = await dialogue_store.resolve_id(scope.id, session_factory=self._factory)
                if resolved is None or self._closed:
                    return None
                scope, mode = resolved
                current = self._owners.get(scope.id)
                if current is not None and (
                    current.mode.revision != mode.revision or current.mode.persona_key != mode.persona_key
                ):
                    predecessor = current.task or current.predecessor
                    await self.cancel_scope(scope.id)
                    current = None
                if current is not None and not current.stopped:
                    return current
                if current is not None:
                    predecessor = current.task or current.predecessor or predecessor
                schedule = await dialogue_store.read_schedule(mode, session_factory=self._factory)
                # SQL IO can span a mode/persona change. Never publish its stale
                # owner over a later revision or attach new input to that owner.
                latest = await dialogue_store.resolve_id(scope.id, session_factory=self._factory)
                if latest is None or self._closed:
                    return None
                if (latest[1].revision, latest[1].persona_key) != (mode.revision, mode.persona_key):
                    continue
                current = _Owner(latest[0], latest[1], schedule, predecessor=predecessor)
                self._owners[scope.id] = current
                return current
            return None

    async def admit(self, session: Session, ctx: Any, *, directed: bool) -> bool:
        if self._closed:
            return False
        self._input_sequence += 1
        incoming = DialogueInput(session, ctx, bool(directed), self._clock(), self._input_sequence)
        await self.start()
        resolved = await dialogue_store.resolve_scope(scope_identity(session), session_factory=self._factory)
        if resolved is None or self._closed:
            return False
        owner = await self._owner(*resolved)
        if owner is None:
            return False
        message_id = str(getattr(getattr(session.event, "message", None), "id", "") or "")
        while not self._closed and not owner.stopped:
            if message_id and message_id in owner.seen_message_ids:
                return True
            # A single reserved direct slot prevents a full ambient backlog from
            # hiding the next urgent safe-boundary input. Total retention remains
            # bounded at max_pending_inputs + 1; subsequent overflow backpressures.
            direct_slot = incoming.directed and not any(item.directed for item in owner.pending)
            if len(owner.pending) < self._max_pending or (direct_slot and len(owner.pending) == self._max_pending):
                owner.pending.append(incoming)
                if message_id:
                    owner.seen_message_ids.add(message_id)
                    owner.message_order.append(message_id)
                    if len(owner.message_order) > 1024:
                        owner.seen_message_ids.discard(owner.message_order.popleft())
                owner.session, owner.ctx = session, ctx
                self._push_pending(owner)
                self.wake()
                return True
            # Backpressure holds the original Session at the caller, rather than
            # dropping or copying an overflowing input into an unbounded side queue.
            owner.space.clear()
            await owner.space.wait()
            if not await dialogue_store.valid(owner.mode, session_factory=self._factory):
                await self.cancel_scope(owner.scope.id)
                return True
        return True

    async def revise(self, session: Session, ctx: Any, *, deleted: bool = False, directed: bool = False) -> None:
        """Change unread input, or fence an old candidate without cancelling started effects."""
        if self._closed or not self._started:
            return
        resolved = await dialogue_store.resolve_scope(scope_identity(session), session_factory=self._factory)
        if resolved is None:
            return
        owner = self._owners.get(resolved[0].id)
        message_id = str(getattr(getattr(session.event, "message", None), "id", "") or "")
        if owner is None or not message_id:
            return
        for index, previous in enumerate(owner.pending):
            source_id = str(getattr(getattr(previous.session.event, "message", None), "id", "") or "")
            if source_id == message_id:
                if deleted:
                    del owner.pending[index]
                    owner.space.set()
                else:
                    owner.pending[index] = DialogueInput(
                        session, ctx, directed, previous.received_at, previous.sequence
                    )
                self.wake()
                return
        batch = owner.batch
        if batch is not None and batch.on_invalidated is not None and batch.on_invalidated(message_id):
            batch._closed = True

    async def drain(self) -> None:
        """Wait for due finite work, never for a future observation or wake alarm."""
        if not self._started:
            return
        self.wake()
        while not self._closed:
            await self._settled.wait()
            tasks = tuple(self._tasks)
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)

    async def offer_opportunity(
        self,
        scope_id: int,
        opportunity: Any,
        *,
        session: Session | None = None,
        ctx: Any = None,
        key: str | None = None,
    ) -> bool:
        if self._closed:
            return False
        await self.start()
        resolved = await dialogue_store.resolve_id(scope_id, session_factory=self._factory)
        if resolved is None or self._closed:
            return False
        owner = await self._owner(*resolved)
        if owner is None:
            return False
        if (
            owner.opportunity is not None
            or (owner.batch is not None and owner.batch.trigger == "opportunity")
            or (key is not None and (key == owner.opportunity_key or key in owner.used_opportunities))
        ):
            return False
        if session is not None and not _matches_scope(owner.scope, session):
            raise ValueError("An opportunity Session belongs to a different native scope")
        owner.opportunity = _Opportunity(opportunity, key, session, ctx)
        self.wake()
        return True

    async def cancel_scope(self, scope_id: int) -> None:
        if not self._started:
            if self._closed:
                return
            await self.start()
        owner = self._owners.get(scope_id)
        expected = owner.schedule if owner is not None else None
        if owner is not None:
            owner.stopped = True
            owner.pending.clear()
            owner.opportunity = None
            owner.admitter = None
            owner.admitted.clear()
            owner.deferred.clear()
            if owner.batch is not None:
                owner.batch._closed = True
            owner.space.set()
            if owner.task is not None and not owner.task.done():
                # A task cancelled before its first step never runs its finally.
                # Let that closed batch enter _execute and settle its own token.
                if owner.executing:
                    owner.task.cancel()
                # The owner drains receipts and finite settlement itself; a
                # competing synchronous SQLite write can deadlock its caller.
                self.wake()
                return
        if owner is not None and owner.predecessor is not None and not owner.predecessor.done():
            self.wake()
            return
        # A stopped idle owner must never clear a replacement claim made during
        # SQL IO. No known schedule means there is no persisted intent we own;
        # an in-progress begin will observe stopped and settle its own token.
        if expected is not None:
            await dialogue_store.cancel(
                scope_id,
                now=self._clock(),
                session_factory=self._factory,
                expected=expected,
            )
        self.wake()

    def _candidate(self, owner: _Owner) -> int:
        return next((index for index, item in enumerate(owner.pending) if item.directed), 0)

    def _fitting_candidate(self, owner: _Owner, budget: int, excluded: set[int] | None = None) -> int | None:
        ambient = None
        for index, item in enumerate(owner.pending):
            if item.characters <= budget and (excluded is None or item.sequence not in excluded):
                if item.directed:
                    return index
                if ambient is None:
                    ambient = index
        return ambient

    def _pop(self, owner: _Owner, index: int) -> DialogueInput:
        item = owner.pending[index]
        del owner.pending[index]
        owner.space.set()
        return item

    def _take(
        self,
        owner: _Owner,
        limit: int,
        budget: int,
        *,
        allow_oversized_root: bool = True,
    ) -> tuple[DialogueInput, ...]:
        taken = []
        characters = 0
        while owner.pending and len(taken) < limit:
            index = (
                (self._candidate(owner) if not taken else 0)
                if allow_oversized_root
                else (self._fitting_candidate(owner, budget - characters))
            )
            if index is None:
                break
            item = owner.pending[index]
            if characters + item.characters > budget and taken:
                break
            # One oversized real root still reaches the runner's existing input
            # limiter; it cannot permanently starve behind this batching budget.
            taken.append(self._pop(owner, index))
            characters += item.characters
            if characters >= budget:
                break
        return tuple(sorted(taken, key=lambda item: item.sequence))

    def _push_pending(self, owner: _Owner) -> None:
        declined: set[int] = set()
        while (
            owner.pending
            and owner.admitter is not None
            and not owner.stopped
            and owner.batch is not None
            and not owner.batch._closed
            and owner.live_count < self._max_live
        ):
            index = self._fitting_candidate(owner, self._max_chars - owner.live_characters, declined)
            if index is None:
                return
            item = owner.pending[index]
            if owner.admitter(item) is not True:
                declined.add(item.sequence)
                continue
            self._pop(owner, index)
            owner.admitted[item.sequence] = item
            owner.live_count += 1
            owner.live_characters += item.characters

    def _due(self, owner: _Owner, now: datetime) -> datetime | None:
        if owner.stopped or owner.faulted or owner.task is not None:
            return None
        if owner.predecessor is not None and not owner.predecessor.done():
            return None
        if owner.pending:
            if any(item.directed for item in owner.pending):
                return now
            due = ambient_deadline(
                min(item.received_at for item in owner.pending),
                max(item.received_at for item in owner.pending),
            )
            if owner.schedule is not None and owner.schedule.not_before is not None:
                due = max(due, owner.schedule.not_before)
            return due
        if owner.transport_blocked:
            return None
        if owner.opportunity is not None:
            return now
        return owner.schedule.wake_at if owner.schedule is not None else None

    async def _launch(self, owner: _Owner) -> None:
        resolved = await dialogue_store.resolve_id(owner.scope.id, session_factory=self._factory)
        if owner.stopped or self._closed or self._owners.get(owner.scope.id) is not owner:
            return
        if resolved is None or (resolved[1].revision, resolved[1].persona_key) != (
            owner.mode.revision,
            owner.mode.persona_key,
        ):
            await self.cancel_scope(owner.scope.id)
            return
        # Live feedback advances context_revision without replacing this owner.
        owner.scope, owner.mode = resolved
        opportunity = owner.opportunity if not owner.pending else None
        anchor = (
            owner.pending[self._candidate(owner)].session
            if owner.pending
            else (opportunity.session if opportunity is not None and opportunity.session is not None else owner.session)
        )
        if self._restore_session is not None and (not owner.pending or anchor is None):
            # Even a retained Session may refer to a disconnected native account.
            # Check fresh availability before consuming a durable no-input intent.
            anchor = await self._restore_session(owner.scope)
        if owner.stopped or self._closed or self._owners.get(owner.scope.id) is not owner:
            return
        if anchor is None:
            owner.transport_blocked = True
            return
        if not _matches_scope(owner.scope, anchor):
            raise ValueError("Restored dialogue transport belongs to a different native scope")
        run_token = await settle_owned(
            dialogue_store.begin(
                owner.mode,
                now=self._clock(),
                session_factory=self._factory,
            )
        )
        if run_token is None:
            owner.faulted = True
            return
        if owner.stopped or self._closed:
            await settle_owned(
                dialogue_store.finish(
                    owner.mode,
                    run_token,
                    DialogueDecision("end"),
                    status="cancelled",
                    confirmed_deliveries=0,
                    now=self._clock(),
                    session_factory=self._factory,
                )
            )
            return
        inputs = self._take(owner, self._max_batch, self._max_chars)
        opportunity = None if inputs else owner.opportunity
        trigger = "input" if inputs else ("opportunity" if opportunity is not None else "wait")
        if inputs:
            root = next((item for item in inputs if item.directed), inputs[0])
            anchor, ctx = root.session, root.ctx
        else:
            ctx = opportunity.ctx if opportunity is not None else owner.ctx
        if opportunity is not None:
            owner.opportunity = None
            owner.opportunity_key = opportunity.key
        owner.schedule = None
        owner.live_count, owner.live_characters = 0, 0
        owner.admitted = {item.sequence: item for item in inputs}
        owner.deferred.clear()

        async def valid() -> bool:
            if self._closed or owner.stopped or owner.batch is not batch or batch._closed:
                return False
            enabled = await dialogue_store.valid(owner.mode, session_factory=self._factory)
            return enabled and not self._closed and not owner.stopped and owner.batch is batch and not batch._closed

        async def pull(limit: int | None) -> tuple[DialogueInput, ...]:
            if not await valid():
                return ()
            remaining = self._max_live - owner.live_count
            if limit is not None:
                if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
                    raise ValueError("Invalid live dialogue input limit")
                remaining = min(remaining, limit)
            budget = self._max_chars - owner.live_characters
            if remaining < 1 or budget < 1 or not owner.pending:
                return ()
            taken = self._take(owner, remaining, budget, allow_oversized_root=False)
            owner.admitted.update((item.sequence, item) for item in taken)
            owner.live_count += len(taken)
            owner.live_characters += sum(item.characters for item in taken)
            return taken

        def bind(callback: Callable[[DialogueInput], bool]) -> None:
            if owner.stopped or self._closed or owner.batch is not batch or batch._closed:
                raise RuntimeError("This dialogue owner is no longer live")
            if owner.admitter is not None:
                raise RuntimeError("This dialogue batch already has an admitter")
            owner.admitter = callback
            self._push_pending(owner)

        def defer(session: Session) -> bool:
            if owner.stopped or self._closed or owner.batch is not batch or batch._closed:
                return False
            for sequence, item in owner.admitted.items():
                if item.session is session:
                    owner.deferred[sequence] = owner.admitted.pop(sequence)
                    return True
            return False

        batch = DialogueBatch(
            owner.scope,
            owner.mode,
            inputs,
            opportunity.value if opportunity is not None else None,
            trigger,
            anchor,
            ctx,
            pull,
            valid,
            bind,
            defer,
        )
        owner.batch = batch
        owner.task = asyncio.create_task(
            self._execute(owner, batch, run_token), name=f"companion-dialogue-{owner.scope.id}"
        )
        self._tasks.add(owner.task)
        owner.task.add_done_callback(self._tasks.discard)

    async def _execute(self, owner: _Owner, batch: DialogueBatch, run_token: str) -> None:
        owner.executing = True
        task = asyncio.current_task()

        def cancelled() -> bool:
            return owner.stopped or self._closed or (task is not None and task.cancelling() > 0)

        outcome = DialogueOutcome(status="failed")
        try:
            if await batch.valid():
                result = await self._runner(batch)
                if not isinstance(result, DialogueOutcome):
                    raise TypeError("The dialogue runner must return DialogueOutcome")
                outcome = result
            else:
                outcome = DialogueOutcome(status="cancelled")
        except asyncio.CancelledError:
            outcome = DialogueOutcome(status="cancelled")
        except Exception:
            _LOGGER.exception(f"Dialogue generation failed for scope {owner.scope.id}")
        finally:
            batch._closed = True
            owner.admitter = None
            try:
                if cancelled():
                    outcome = DialogueOutcome(status="cancelled", confirmed_deliveries=outcome.confirmed_deliveries)
                owner.schedule = await settle_owned(
                    dialogue_store.finish(
                        owner.mode,
                        run_token,
                        outcome.decision,
                        status=outcome.status,
                        confirmed_deliveries=outcome.confirmed_deliveries,
                        now=self._clock(),
                        session_factory=self._factory,
                        cancelled=cancelled,
                    )
                )
                # Cancellation can land while the completed transaction commits.
                # Only this exact settlement may be terminalized, after receipt
                # drain and without racing a replacement owner's running token.
                if cancelled() and owner.schedule is not None and owner.schedule.status != "cancelled":
                    await settle_owned(
                        dialogue_store.cancel(
                            owner.scope.id,
                            now=self._clock(),
                            session_factory=self._factory,
                            expected=owner.schedule,
                        )
                    )
                    owner.schedule = None
            except Exception:
                owner.faulted = True
                _LOGGER.exception(f"Dialogue settlement failed for scope {owner.scope.id}; work will not replay")
            finally:
                if owner.opportunity_key is not None:
                    owner.used_opportunities.append(owner.opportunity_key)
                owner.opportunity_key = None
                if owner.deferred and not owner.stopped and not self._closed and not owner.faulted:
                    # At most one batch + its live allowance is held separately;
                    # keep original Sessions/context and never readmit mid-batch.
                    owner.pending = deque(
                        sorted(
                            (*owner.pending, *owner.deferred.values()),
                            key=lambda item: item.sequence,
                        )
                    )
                owner.admitted.clear()
                owner.deferred.clear()
                owner.executing = False
                owner.task, owner.batch = None, None
                owner.space.set()
                self.wake()

    async def _run_loop(self) -> None:
        while not self._closed:
            self._wake.clear()
            try:
                now = self._clock()
                for owner in tuple(self._owners.values()):
                    due = self._due(owner, now)
                    if due is not None and due <= now:
                        await self._launch(owner)
                deadlines = [
                    due for owner in self._owners.values() if (due := self._due(owner, self._clock())) is not None
                ]
                self._settled.set()
                timeout = max(0.0, (min(deadlines) - self._clock()).total_seconds()) if deadlines else None
                if timeout is None:
                    await self._wake.wait()
                else:
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout)
                    except TimeoutError:
                        pass
            except asyncio.CancelledError:
                return
            except Exception:
                _LOGGER.exception("Dialogue scheduling failed; waiting for the next real wakeup")
                self._settled.set()
                await self._wake.wait()

    async def aclose(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._wake.set()
            self._settled.set()
            if self._loop is not None:
                self._loop.cancel()
            tasks = tuple(self._tasks)
            for owner in self._owners.values():
                owner.stopped = True
                owner.admitter = None
                owner.admitted.clear()
                owner.deferred.clear()
                owner.pending.clear()
                owner.opportunity = None
                owner.space.set()
                if owner.batch is not None:
                    owner.batch._closed = True
                if owner.task is not None and owner.executing:
                    owner.task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self._loop is not None:
                await asyncio.gather(self._loop, return_exceptions=True)


def _matches_scope(scope: ChatScope, session: Session) -> bool:
    identity = scope_identity(session)
    return (identity.platform, identity.account_id, identity.channel_id) == (
        scope.platform,
        scope.account_id,
        scope.channel_id,
    )
