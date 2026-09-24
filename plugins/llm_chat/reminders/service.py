"""Database-owned scheduled reminder wakeups and generation workers."""

from __future__ import annotations

import sys
from copy import deepcopy
from typing import Any
import asyncio
from datetime import datetime, timezone
from collections.abc import Callable, Iterable, Awaitable

from satori import User, Event, Guild, Channel, ChannelType, LoginStatus
from launart import Launart, Service
from arclet.entari import Session
from launart.status import Phase
from arclet.entari.const import ITEM_SESSION
from arclet.entari.logger import log
from arclet.entari.event.base import InternalEvent
from arclet.letoderea.context import Contexts
from entari_plugin_llm.config import get_model_config

from utils.llm_model_core.snapshot import pin_main_model, main_model_scope

from .store import (
    ReminderClaim,
    ReminderConflict,
    renew_claim,
    expire_tasks,
    bind_turn_ref,
    due_task_refs,
    recover_stale,
    try_claim_due,
    finalize_claim,
    get_occurrence,
    get_preference,
    get_task_guild_id,
    get_agent_turn_ref,
    get_task_for_service,
    record_model_started,
    record_external_started,
)
from .runtime import reminder_decision_scope, current_reminder_decision
from .delivery import ReminderLeaseLost, reminder_transport_scope
from ..identity import ChatIdentity
from ..image_inputs import ImageInputs, image_inputs_scope
from ..channel_turns import participant_turn_active
from ..delivery_audit import delivery_audit_scope
from ..group_delivery import group_delivery_scope
from ..session_manager import migrate_turn_origin_columns
from ..agent_turn_setup import prepare_scheduled_agent_turn

_LOGGER = log.wrapper("[llm_chat.reminders]")
AccountKey = tuple[str, str]
Runner = Callable[..., Awaitable[str]]


def _online(account: Any) -> bool:
    connected = getattr(account, "connected", None)
    if connected is not None and connected.is_set():
        return True
    protocol = getattr(account, "protocol", None)
    server = getattr(protocol, "server", None)
    if server is None:
        return False
    from entari_plugin_server.patch import DirectAdapterServer

    return isinstance(server, DirectAdapterServer) and getattr(account.self_info, "status", None) == LoginStatus.ONLINE


def _account_key(account: Any) -> AccountKey:
    return str(account.platform), str(account.self_id)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def build_scheduled_session(account: Any, task: Any, channel: Channel, guild_id: str = "") -> Session:
    """Create a native transport Session with no fabricated inbound message or ID."""
    if not channel.id or not task.platform_user_id:
        raise ValueError("scheduled target lacks a channel or user")
    event = InternalEvent(
        account,
        Event(
            type="internal",
            timestamp=datetime.now(timezone.utc),
            login=account.self_info,
            channel=channel,
            guild=Guild(id=guild_id) if guild_id and channel.type != ChannelType.DIRECT else None,
            user=User(id=task.platform_user_id),
            message=None,
        ),
    )
    return Session(account, event)


class ReminderService(Service):
    id = "llm_chat.reminders.service"

    def __init__(
        self,
        config: Any,
        *,
        accounts: Callable[[], Iterable[Any]] | None = None,
        runner: Runner | None = None,
        dm_channel: Callable[[Any, str], Awaitable[Channel]] | None = None,
        concurrency: int = 4,
        lease_seconds: int = 300,
    ) -> None:
        super().__init__()
        self.config = config
        self._accounts_provider = accounts
        self._runner = runner
        self._dm_channel = dm_channel or self._native_dm_channel
        self._capacity = max(1, min(4, concurrency))
        self._lease_seconds = lease_seconds
        self._accounts: dict[AccountKey, Any] = {}
        self._wake = asyncio.Event()
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._claims: dict[str, ReminderClaim] = {}
        self._loop: asyncio.Task[None] | None = None
        self._closed = False
        self._started = False
        self._start_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()

    @property
    def required(self) -> set[str]:
        return {"database/sqlalchemy"}

    @property
    def stages(self) -> set[Phase]:
        return {"preparing", "blocking", "cleanup"}

    @staticmethod
    async def _native_dm_channel(account: Any, user_id: str) -> Channel:
        return await account.protocol.user_channel_create(user_id=user_id)

    def _refresh_accounts(self) -> None:
        if self._accounts_provider is None:
            return
        self._accounts = {_account_key(account): account for account in self._accounts_provider() if _online(account)}

    def account_update(self, account: Any) -> None:
        if self._closed:
            return
        key = _account_key(account)
        if _online(account):
            self._accounts[key] = account
            self.wake()
        elif self._accounts.get(key) is account:
            self._accounts.pop(key, None)

    def wake(self) -> None:
        """A heartbeat only signals the worker loop; it performs no I/O itself."""
        if not self._closed:
            self._wake.set()

    async def start(self) -> None:
        async with self._start_lock:
            if self._started or self._closed:
                return
            await migrate_turn_origin_columns()
            await recover_stale()
            await expire_tasks()
            self._refresh_accounts()
            if self._closed:
                return
            self._started = True
            self._loop = asyncio.create_task(self._run_loop(), name="reminder-wakeup-loop")
            self.wake()

    async def _run_loop(self) -> None:
        while not self._closed:
            await self._wake.wait()
            self._wake.clear()
            try:
                await recover_stale()
                await expire_tasks()
                await self._reconcile_workers()
                await self.claim_ready()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _LOGGER.warning(f"reminder wakeup failed: {type(exc).__name__}")

    async def _reconcile_workers(self) -> None:
        for task_ref, worker in tuple(self._workers.items()):
            if worker.done():
                continue
            claim = self._claims.get(task_ref)
            if claim is None:
                continue
            task = await get_task_for_service(task_ref)
            occurrence = await get_occurrence(claim)
            if (
                task is None
                or task.revision != claim.revision
                or task.status != "active"
                or occurrence is None
                or occurrence.lease_token != claim.lease_token
                or occurrence.state not in {"claimed", "model_started", "dm_started", "transport_started", "confirmed"}
            ):
                worker.cancel()

    async def claim_ready(self) -> None:
        """Claim only currently connected accounts, in short database transactions."""
        if self._closed:
            return
        self._refresh_accounts()
        capacity = self._capacity - len(self._workers)
        if capacity <= 0:
            return
        for key, account in tuple(self._accounts.items()):
            if capacity <= 0 or self._closed:
                return
            if not _online(account):
                continue
            for task_ref in await due_task_refs(platform=key[0], account_id=key[1], limit=100):
                if capacity <= 0 or self._closed or not _online(account):
                    break
                if task_ref in self._workers:
                    continue
                task = await get_task_for_service(task_ref)
                if task is None or task.platform != key[0] or task.account_id != key[1]:
                    continue
                if task.origin != "user":
                    preference = await get_preference(account_id=key[1], unified_user_id=task.unified_user_id)
                    if preference is not None and not preference.enabled:
                        continue
                target = task.delivery_channel_id if task.delivery_target_type == "channel" else task.source_channel_id
                if participant_turn_active(*key, target, task.platform_user_id):
                    continue
                claim = await try_claim_due(
                    task_ref, platform=key[0], account_id=key[1], lease_seconds=self._lease_seconds
                )
                if claim is None:
                    continue
                worker = asyncio.create_task(self._run_claim(claim, account), name=f"reminder-{claim.task_ref}")
                self._workers[claim.task_ref] = worker
                self._claims[claim.task_ref] = claim
                worker.add_done_callback(lambda done, ref=claim.task_ref: self._finished(ref, done))
                capacity -= 1

    def _finished(self, task_ref: str, worker: asyncio.Task[None]) -> None:
        if self._workers.get(task_ref) is worker:
            self._workers.pop(task_ref, None)
            self._claims.pop(task_ref, None)
        if not worker.cancelled():
            try:
                worker.result()
            except Exception as exc:
                _LOGGER.warning(f"reminder worker failed: {type(exc).__name__}")
        self.wake()

    async def _renew_loop(self, claim: ReminderClaim, worker: asyncio.Task[Any]) -> None:
        while True:
            await asyncio.sleep(max(1, self._lease_seconds // 3))
            try:
                if await renew_claim(claim, lease_seconds=self._lease_seconds):
                    continue
            except Exception as exc:
                _LOGGER.warning(f"reminder lease renewal failed: {type(exc).__name__}")
            worker.cancel()
            return

    async def _run_claim(self, claim: ReminderClaim, account: Any) -> None:
        worker = asyncio.current_task()
        assert worker is not None
        renewer = asyncio.create_task(self._renew_loop(claim, worker))
        prepared = None
        audit = None
        result = "failed"
        failure = "preparation_failed"
        try:
            task = await get_task_for_service(claim.task_ref)
            if task is None or task.revision != claim.revision or not _online(account):
                failure = "target_offline_or_changed"
                return
            if task.delivery_target_type == "dm":
                if (
                    not task.dm_authorized
                    or not task.platform_user_id
                    or task.delivery_user_id != task.platform_user_id
                ):
                    failure = "dm_not_authorized"
                    return
                if not await renew_claim(claim) or not await record_external_started(claim, kind="dm"):
                    raise ReminderLeaseLost("DM resolution claim revoked")
                failure = "dm_target_unknown"
                channel = await self._dm_channel(account, task.delivery_user_id)
                if not isinstance(channel, Channel) or not channel.id or channel.type != ChannelType.DIRECT:
                    raise ValueError("DM resolution did not return a direct channel")
            elif task.delivery_target_type == "channel" and task.delivery_channel_id:
                channel = Channel(id=task.delivery_channel_id, type=ChannelType.TEXT)
            else:
                failure = "invalid_delivery_target"
                return
            guild_id = await get_task_guild_id(task.source_scope_id)
            session = build_scheduled_session(account, task, channel, guild_id)
            ctx = Contexts({ITEM_SESSION: session, "$event": session.event})
            await session.event.gather(ctx)
            inputs = ImageInputs(warn=_LOGGER.warning)
            turn_config = deepcopy(self.config)
            now = datetime.now(timezone.utc)
            facts = {
                "scheduled_at": _utc(task.due_at).isoformat(),
                "now": now.isoformat(),
                "late_by_seconds": max(0, int((now - _utc(task.due_at)).total_seconds())),
                "occurrence_sequence": claim.occurrence_sequence,
                "background": task.background,
                "continuation_state": task.continuation_state,
            }
            with (
                group_delivery_scope(session, scheduled_reminder=True),
                delivery_audit_scope(session, _LOGGER.warning, scheduled_reminder=True) as audit,
                main_model_scope(),
                image_inputs_scope(inputs),
                reminder_decision_scope(),
                reminder_transport_scope(claim),
            ):
                try:
                    try:
                        model_name = pin_main_model(get_model_config(turn_config.model, channel.id)).name
                    except Exception as exc:
                        _LOGGER.warning(f"reminder model resolution failed: {type(exc).__name__}")
                        failure = "model_unavailable"
                        return
                    identity = ChatIdentity(task.unified_user_id, task.platform_user_id, "")

                    prepared = await prepare_scheduled_agent_turn(
                        turn_config,
                        session,
                        identity,
                        model_name=model_name,
                        task_ref=task.task_ref,
                        content=task.goal,
                        scheduled_context=facts,
                        image_inputs=inputs,
                        warn=_LOGGER.warning,
                        tool_schemas=getattr(sys.modules[__package__.rsplit(".", 1)[0]], "registered_tool_schemas", ()),
                    )
                    if not await bind_turn_ref(claim, await get_agent_turn_ref(prepared.lifecycle.agent_turn_id)):
                        raise ReminderLeaseLost("claim changed before model")
                    if not await record_model_started(claim):
                        raise ReminderLeaseLost("claim changed before model")
                    failure = "model_failed"
                    if self._runner is None:
                        from ..chat_handler import run_prepared_agent_turn

                        runner = run_prepared_agent_turn
                    else:
                        runner = self._runner
                    result = await runner(
                        session, ctx, prepared, model_name=model_name, turn_config=turn_config, reaction=None
                    )
                    decision = current_reminder_decision()
                    occurrence = await get_occurrence(claim)
                    if occurrence is None:
                        raise ReminderLeaseLost("occurrence disappeared")
                    if occurrence.transport_started and occurrence.confirmed_count == 0:
                        failure = "transport_result_unknown"
                    elif result not in {"completed", "silent", "declined"}:
                        failure = "turn_failed"
                    elif decision is None or decision.task_ref != claim.task_ref:
                        failure = "missing_reminder_decision"
                    elif decision.status == "active" and (
                        decision.next_due_at is None or decision.next_due_at <= datetime.now(timezone.utc)
                    ):
                        failure = "invalid_next_schedule"
                    else:
                        failure = ""
                        await self._settle(
                            claim,
                            prepared,
                            status=decision.status,
                            next_due_at=decision.next_due_at,
                            continuation_state=decision.continuation_state,
                        )
                finally:
                    await audit.drain()
                    await inputs.aclose()
        except asyncio.CancelledError:
            failure = "interrupted_after_external_boundary"
            raise
        except ReminderConflict:
            return
        except Exception as exc:
            _LOGGER.warning(f"reminder execution failed: {type(exc).__name__}")
        finally:
            renewer.cancel()
            await asyncio.gather(renewer, return_exceptions=True)
            if failure:
                try:
                    settle = asyncio.create_task(self._settle(claim, prepared, status="paused", reason=failure))
                    await asyncio.shield(settle)
                except asyncio.CancelledError:
                    await settle
                    raise
                except ReminderConflict:
                    pass  # cancellation or replacement has won the revision CAS
                except Exception as exc:
                    _LOGGER.warning(f"reminder settlement failed: {type(exc).__name__}")

    async def _settle(
        self,
        claim: ReminderClaim,
        prepared: Any,
        *,
        status: str,
        next_due_at: datetime | None = None,
        continuation_state: str | None = None,
        reason: str = "",
    ) -> None:
        task = await finalize_claim(
            claim, status=status, next_due_at=next_due_at, continuation_state=continuation_state, failure_reason=reason
        )
        if prepared is not None:
            prepared.agent_events.append(
                "reminder_decision",
                model_visible=False,
                status="confirmed" if status != "paused" else "failed",
                effect="confirmed" if status != "paused" else "none",
                payload={
                    "task_ref": claim.task_ref,
                    "status": task.status,
                    "next_due_at": _utc(task.due_at).isoformat() if status == "active" else "",
                    "revision": task.revision,
                    "reason": reason,
                },
            )
            await prepared.agent_events.flush()

    async def aclose(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._wake.set()
            loop = self._loop
            if loop is not None:
                loop.cancel()
                await asyncio.gather(loop, return_exceptions=True)
            workers = tuple(self._workers.values())
            for worker in workers:
                worker.cancel()
            if workers:
                await asyncio.gather(*workers, return_exceptions=True)
            self._workers.clear()
            self._claims.clear()
            self._accounts.clear()

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
