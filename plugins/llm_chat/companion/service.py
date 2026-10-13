"""Native, database-owned lifecycle for opt-in autonomous companionship."""

from __future__ import annotations

from copy import deepcopy
import math
from typing import Any
import asyncio
from datetime import datetime, timezone, timedelta
from dataclasses import field, dataclass
from collections.abc import Mapping, Callable, Iterable, Sequence, Awaitable

from launart import Launart, Service
from sqlalchemy import select
from arclet.entari import Session
from launart.status import Phase
from arclet.entari.logger import log
from entari_plugin_database import get_session

from utils.companion_core import ReflectionProposal, next_review_at

from . import store
from .types import ReviewClaim, ContactClaim, CompanionConflict
from .models import CompanionMode, CompanionReview, CompanionContact
from ..config import LLMChatConfig
from ..models import AgentTurn, ChatScope, ContextSession, ScopePersonaSelection
from .contact import run_contact
from .context import companion_limits
from .accounts import AccountAvailability
from .internal import settle_owned, run_reflection
from ..personality import persona_scope_lock, select_scope_persona, resolve_scope_persona
from ..channel_turns import participant_turn_active
from .decision_audit import ContactDecision, persist_review_decision
from ..session_manager import finish_turn
from ..agent_turn_setup import PreparedAgentTurn

_LOGGER = log.wrapper("[llm_chat.companion]")
AccountKey = tuple[str, str]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _key(account: Any) -> AccountKey:
    return str(account.platform), str(account.self_id)


@dataclass(slots=True)
class _Worker:
    scope: ChatScope
    account: Any
    review: ReviewClaim
    input_revision: int
    task: asyncio.Task[None] | None = None
    contact: ContactClaim | None = None
    members: frozenset[str] = field(default_factory=frozenset)
    snapshot_loaded: bool = False
    review_committing: bool = False
    stopped: bool = False


class CompanionService(Service):
    id = "llm_chat.companion.service"

    def __init__(
        self,
        config: LLMChatConfig,
        tool_schemas: Sequence[Mapping[str, object]] = (),
        *,
        accounts: Callable[[], Iterable[Any]] | None = None,
        reflection: Callable[..., Awaitable[ReflectionProposal]] | None = None,
        runner: Callable[..., Awaitable[str]] | None = None,
        clock: Callable[[], datetime] = _now,
        concurrency: int = 2,
    ) -> None:
        super().__init__()
        self.config = config
        self.tool_schemas = tuple(tool_schemas)
        self._accounts_provider = accounts
        self._reflection = reflection or run_reflection
        self._runner = runner
        self._clock = clock
        self._capacity = max(1, min(4, concurrency))
        self._accounts: dict[AccountKey, Any] = {}
        self._availability = AccountAvailability()
        self._workers: dict[int, _Worker] = {}
        self._input_revisions: dict[tuple[str, str, str], int] = {}
        self._wake = asyncio.Event()
        self._loop: asyncio.Task[None] | None = None
        self._startup: asyncio.Task[None] | None = None
        self._started = False
        self._closed = False
        self._start_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._claim_lock = asyncio.Lock()

    @property
    def required(self) -> set[str]:
        return {"database/sqlalchemy"}

    @property
    def stages(self) -> set[Phase]:
        return {"preparing", "blocking", "cleanup"}

    def wake(self) -> None:
        if not self._closed:
            self._wake.set()

    @staticmethod
    def _stop(worker: _Worker) -> None:
        if worker.stopped:
            return
        worker.stopped = True
        if worker.task is not None and not worker.task.done():
            worker.task.cancel()

    def account_update(self, account: Any) -> None:
        if self._closed:
            return
        key = _key(account)
        previous = self._accounts.get(key)
        if previous is not None and previous is not account:
            for worker in tuple(self._workers.values()):
                if worker.account is previous:
                    self._stop(worker)
        self._accounts[key] = account
        self._availability.clear()
        self.wake()

    def interrupt_input(self, session: Session) -> None:
        """Synchronously fence all old ideas for this addressed account/channel."""
        key = _key(session.account)
        channel_id = str(session.channel.id)
        scope_key = (*key, channel_id)
        self._input_revisions[scope_key] = self._input_revisions.get(scope_key, 0) + 1
        for worker in tuple(self._workers.values()):
            if _key(worker.account) == key and worker.scope.channel_id == channel_id:
                self._stop(worker)

    async def cancel_scope(self, scope_id: int) -> None:
        worker = self._workers.get(scope_id)
        if worker is not None:
            self._stop(worker)
        # Cancellation is immediate; draining here under a persona lock would deadlock preparation.
        self.wake()

    async def start(self) -> None:
        async with self._start_lock:
            if self._started or self._closed:
                return
            self._startup = asyncio.create_task(self._initialize(), name="companion-startup")
            try:
                await self._startup
            finally:
                self._startup = None

    async def _initialize(self) -> None:
        await store.initialize_store(now=self._clock())
        await self._recover_native_turns()
        if self._closed:
            return
        self._started = True
        self._loop = asyncio.create_task(self._run_loop(), name="companion-wakeup-loop")
        self.wake()

    async def _recover_native_turns(self) -> None:
        """Close abandoned internal audit rows without replaying generation or transport."""
        moment = self._clock().astimezone(timezone.utc).replace(tzinfo=None)
        abandoned: list[int] = []
        async with get_session() as db:
            rows = (
                await db.execute(
                    select(AgentTurn, ContextSession)
                    .join(ContextSession, ContextSession.id == AgentTurn.session_id)
                    .where(
                        AgentTurn.status == "running", ContextSession.purpose.in_(("companion", "companion_reflection"))
                    )
                )
            ).all()
            for turn, context in rows:
                if context.scope_id in self._workers:
                    continue
                if (context.purpose, turn.trigger_kind) == ("companion_reflection", "reflection"):
                    live = await db.scalar(
                        select(CompanionReview.lease_token)
                        .where(
                            CompanionReview.scope_id == context.scope_id,
                            CompanionReview.status == "active",
                            CompanionReview.lease_deadline > moment,
                        )
                        .limit(1)
                    )
                elif (context.purpose, turn.trigger_kind) == ("companion", "autonomous"):
                    live = await db.scalar(
                        select(CompanionContact.attempt_ref)
                        .where(
                            CompanionContact.scope_id == context.scope_id,
                            CompanionContact.status == "active",
                            CompanionContact.lease_deadline > moment,
                        )
                        .limit(1)
                    )
                else:
                    continue
                if live is None:
                    abandoned.append(turn.id)
        for turn_id in abandoned:
            await finish_turn(turn_id, status="cancelled")

    async def _repair_personas(self) -> None:
        async with get_session() as db:
            rows = (
                await db.execute(
                    select(CompanionMode.scope_id, CompanionMode.persona_key, ScopePersonaSelection.persona_key)
                    .outerjoin(ScopePersonaSelection, ScopePersonaSelection.scope_id == CompanionMode.scope_id)
                    .where(CompanionMode.mode == "companion")
                )
            ).all()
        for scope_id, previous, selected in rows:
            if previous == selected and selected in self.config.personas:
                continue
            worker = self._workers.get(scope_id)
            if worker is not None:
                self._stop(worker)
            async with persona_scope_lock(scope_id):
                persona = await resolve_scope_persona(self.config, scope_id)
                if selected != persona.key:
                    await select_scope_persona(self.config, scope_id, persona.key)
                await store.rebind_persona(scope_id, persona.key, now=self._clock())

    def _refresh_accounts(self) -> None:
        if self._accounts_provider is None:
            return
        current = {_key(account): account for account in self._accounts_provider()}
        for worker in tuple(self._workers.values()):
            if current.get(_key(worker.account)) is not worker.account:
                self._stop(worker)
        self._accounts = current

    def _owned_current(self, worker: _Worker) -> bool:
        scope = worker.scope
        revision = self._input_revisions.get((scope.platform, scope.account_id, scope.channel_id), 0)
        return not self._closed and not worker.stopped and revision == worker.input_revision

    async def _review_current(self, worker: _Worker) -> bool:
        if not self._owned_current(worker):
            return False
        if worker.review_committing:
            mode = await store.get_mode(worker.scope.id)
            if (mode.mode, mode.persona_key, mode.revision, mode.context_revision) != (
                "companion",
                worker.review.persona_key,
                worker.review.mode_revision,
                worker.review.context_revision,
            ):
                return False
            members = await store.read_members(
                worker.scope.id,
                worker.review.persona_key,
                now=self._clock(),
                member_idle_days=companion_limits(self.config).member_idle_days,
            )
            return self._owned_current(worker) and worker.members.issubset({member.member_ref for member in members})
        try:
            snapshot = await store.load_reflection(
                worker.review,
                now=self._clock(),
                member_idle_days=companion_limits(self.config).member_idle_days,
            )
        except CompanionConflict:
            return False
        if worker.snapshot_loaded and not worker.members.issubset({member.member_ref for member in snapshot.members}):
            return False
        return self._owned_current(worker)

    async def _contact_current(self, worker: _Worker) -> bool:
        claim = worker.contact
        if not self._owned_current(worker) or claim is None:
            return False
        if not await store.contact_valid(claim, now=self._clock()):
            return False
        persona = await resolve_scope_persona(self.config, worker.scope.id)
        if persona.key != claim.persona_key:
            return False
        if participant_turn_active(
            worker.scope.platform,
            worker.scope.account_id,
            worker.scope.channel_id,
            claim.platform_user_id,
        ):
            return False
        if self._accounts_provider is not None:
            self._refresh_accounts()
            if self._accounts.get(_key(worker.account)) is not worker.account:
                return False
        if not await self._availability.online(worker.account, fresh=True):
            return False
        return self._owned_current(worker)

    async def _reconcile_workers(self) -> None:
        for worker in tuple(self._workers.values()):
            if worker.stopped or worker.task is None or worker.task.done():
                continue
            valid = await self._contact_current(worker) if worker.contact else await self._review_current(worker)
            if valid and worker.contact is None:
                valid = await self._availability.online(worker.account)
            if not valid:
                self._stop(worker)

    async def claim_ready(self) -> None:
        """Use native status probes only after discovering an enabled due scope."""
        if self._closed or not self._started:
            return
        async with self._claim_lock:
            await self._repair_personas()
            await self._reconcile_workers()
            capacity = self._capacity - len(self._workers)
            if capacity <= 0 or self._closed:
                return
            due = await store.due_scopes(now=self._clock(), limit=100)
            if not due:
                return
            self._refresh_accounts()
            limits = companion_limits(self.config)
            lease_seconds = min(3600, max(180, math.ceil(limits.request_timeout) + 120))
            for mode in due:
                if capacity <= 0 or self._closed:
                    break
                if mode.scope_id in self._workers:
                    continue
                async with get_session() as db:
                    scope = await db.get(ChatScope, mode.scope_id)
                if scope is None:
                    continue
                account = self._accounts.get((scope.platform, scope.account_id))
                if account is None or not await self._availability.online(account):
                    continue
                input_revision = self._input_revisions.get((scope.platform, scope.account_id, scope.channel_id), 0)
                claim = await store.claim_review(
                    scope.id,
                    mode.persona_key,
                    now=self._clock(),
                    lease_seconds=lease_seconds,
                    daily_limit=limits.daily_reflections,
                )
                if claim is None:
                    continue
                worker = _Worker(scope, account, claim, input_revision)
                task = asyncio.create_task(self._run_claim(worker), name=f"companion-{scope.id}")
                worker.task = task
                self._workers[scope.id] = worker
                task.add_done_callback(lambda done, owned=worker: self._finished(owned, done))
                capacity -= 1

    def _finished(self, worker: _Worker, task: asyncio.Task[None]) -> None:
        if self._workers.get(worker.scope.id) is worker:
            self._workers.pop(worker.scope.id, None)
        if not task.cancelled():
            try:
                task.result()
            except Exception as exc:
                _LOGGER.warning(f"companion worker failed: {type(exc).__name__}")
        self.wake()

    async def _run_claim(self, worker: _Worker) -> None:
        claim = worker.review
        config = deepcopy(self.config)
        limits = companion_limits(config)
        review_finished = False
        failure = "reflection_failed"
        decision = ContactDecision()
        try:
            snapshot = await store.load_reflection(
                claim,
                now=self._clock(),
                member_idle_days=limits.member_idle_days,
            )
            worker.members = frozenset(member.member_ref for member in snapshot.members)
            worker.snapshot_loaded = True
            if any(
                participant_turn_active(
                    worker.scope.platform,
                    worker.scope.account_id,
                    worker.scope.channel_id,
                    member.platform_user_id,
                )
                for member in snapshot.members
            ):
                failure = "reflection_interrupted"
                decision.set("interrupted", "participant_active", at=self._clock())
                return
            if not await self._review_current(worker):
                decision.set("blocked", "mode_changed", at=self._clock())
                raise CompanionConflict("Companion reflection was revoked before generation")
            if not await self._availability.online(worker.account):
                decision.set("blocked", "account_unavailable", at=self._clock())
                raise CompanionConflict("Companion reflection was revoked before generation")
            proposal = await asyncio.wait_for(
                self._reflection(
                    config=config,
                    scope=worker.scope,
                    claim=claim,
                    snapshot=snapshot,
                    limits=limits,
                    now=self._clock(),
                    tool_schemas=self.tool_schemas,
                ),
                timeout=limits.request_timeout + 30,
            )
            if not isinstance(proposal, ReflectionProposal):
                raise TypeError("Companion reflection must return a validated ReflectionProposal")
            if not await self._review_current(worker):
                decision.set("blocked", "mode_changed", at=self._clock())
                raise CompanionConflict("Companion reflection changed before state commit")
            committed_at = self._clock()
            worker.review_committing = True
            review_finished = await store.finish_review(
                claim,
                proposal,
                now=committed_at,
                next_review_at=next_review_at(committed_at, proposal.review_after_minutes, limits=limits),
            )
            if not review_finished:
                decision.set("blocked", "state_not_committed", at=self._clock())
                return
            if self._closed or worker.stopped:
                decision.set("interrupted", "service_stopped", at=self._clock())
                return
            if proposal.contact is None:
                decision.set("not_proposed", "no_contact_proposed", at=self._clock())
                return
            current = await store.get_mode(claim.scope_id)
            if (current.mode, current.persona_key, current.revision, current.context_revision) != (
                "companion",
                claim.persona_key,
                claim.mode_revision,
                claim.context_revision,
            ):
                decision.set("blocked", "mode_changed", at=self._clock())
                return
            if participant_turn_active(
                worker.scope.platform,
                worker.scope.account_id,
                worker.scope.channel_id,
                next(
                    (
                        member.platform_user_id
                        for member in snapshot.members
                        if member.member_ref == proposal.contact.member_ref
                    ),
                    "",
                ),
            ):
                decision.set("blocked", "participant_active", at=self._clock())
                return
            contact = await store.claim_contact(
                claim.scope_id,
                claim.persona_key,
                proposal.contact.member_ref,
                proposal.contact,
                origin=claim,
                now=self._clock(),
                limits=limits,
                lease_seconds=300,
                decision=decision,
            )
            if contact is None:
                return
            worker.contact = contact
            await run_contact(
                config=config,
                scope=worker.scope,
                account=worker.account,
                claim=contact,
                proposal=proposal.contact,
                guard=lambda: self._contact_current(worker),
                clock=self._clock,
                tool_schemas=self.tool_schemas,
                runner=self._runner,
            )
        except asyncio.CancelledError:
            failure = "reflection_interrupted"
            if decision.status != "reserved":
                decision.set("interrupted", "reflection_interrupted", at=self._clock())
            raise
        except Exception as exc:
            failure = type(exc).__name__
            if decision.status == "failed":
                decision.set("failed", failure, at=self._clock())
            _LOGGER.warning(f"companion scope execution failed: {failure}")
        finally:

            async def audit_decision() -> None:
                try:
                    await persist_review_decision(claim, decision, now=self._clock(), session_factory=get_session)
                except Exception as exc:
                    _LOGGER.warning(f"companion decision audit failed: {type(exc).__name__}")

            try:
                if not review_finished:
                    now = self._clock()
                    retry = (
                        now + timedelta(minutes=1)
                        if failure == "reflection_interrupted"
                        else next_review_at(
                            now,
                            limits.reflection_interval_minutes,
                            limits=limits,
                        )
                    )
                    await settle_owned(store.fail_review(claim, now=now, retry_at=retry, reason=failure))
            finally:
                await settle_owned(audit_decision())

    async def record_chat(self, session: Session, prepared: PreparedAgentTurn) -> None:
        """Record only completed native ordinary-chat feedback under its captured mode."""
        access = prepared.agent_access
        if (
            self._closed
            or prepared.conversation_mode != "companion"
            or access.trigger_kind != "message"
            or not access.turn_id
        ):
            return
        async with persona_scope_lock(access.scope_id):
            current = await store.get_mode(access.scope_id)
            if (current.mode, current.persona_key, current.revision) != (
                "companion",
                prepared.persona.key,
                prepared.companion_revision,
            ):
                return
            async with get_session() as db:
                scope = await db.get(ChatScope, access.scope_id)
                turn = await db.get(AgentTurn, access.turn_id)
            if (
                scope is None
                or (scope.platform, scope.account_id, scope.channel_id)
                != (
                    *_key(session.account),
                    str(session.channel.id),
                )
                or turn is None
                or turn.status not in {"completed", "silent", "declined"}
                or turn.finished_at is None
            ):
                return
            member = await store.record_chat(
                access.scope_id,
                prepared.persona.key,
                user_id=access.user_id,
                platform_user_id=str(session.user.id),
                display_name=str(session.user.name or ""),
                turn_id=access.turn_id,
                now=self._clock(),
                expected_revision=prepared.companion_revision,
            )
        if member is not None:
            current = await store.get_mode(access.scope_id)
            worker = self._workers.get(access.scope_id)
            if worker is not None:
                claim = worker.contact or worker.review
                if (claim.persona_key, claim.mode_revision, claim.context_revision) != (
                    current.persona_key,
                    current.revision,
                    current.context_revision,
                ):
                    await self.cancel_scope(access.scope_id)
            self.wake()

    async def _run_loop(self) -> None:
        while not self._closed:
            await self._wake.wait()
            self._wake.clear()
            try:
                await store.recover_stale(now=self._clock())
                await self._recover_native_turns()
                await self.claim_ready()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _LOGGER.warning(f"companion wakeup failed: {type(exc).__name__}")

    async def drain(self) -> None:
        """Wait only for finite work already owned, not the heartbeat loop."""
        tasks = tuple(worker.task for worker in self._workers.values() if worker.task is not None)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def aclose(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._wake.set()
            for task in (self._startup, self._loop):
                if task is not None:
                    task.cancel()
                    await settle_owned(asyncio.gather(task, return_exceptions=True))
            for worker in tuple(self._workers.values()):
                self._stop(worker)
            await settle_owned(self.drain())
            self._workers.clear()
            self._accounts.clear()
            self._input_revisions.clear()
            self._availability.clear()

    async def launch(self, manager: Launart) -> None:
        async with self.stage("preparing"):
            database = manager.get_component("database/sqlalchemy")
            if self._accounts_provider is None:
                self._accounts_provider = lambda: manager.get_component("entari.service").accounts.values()
        async with self.stage("blocking"):
            await database.status.wait_for("blocking")
            await self.start()
            await manager.status.wait_for_sigexit()
        async with self.stage("cleanup"):
            await self.aclose()
