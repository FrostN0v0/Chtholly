"""Scheduled reminder worker, account and transport lifecycle regressions."""

from __future__ import annotations

from types import SimpleNamespace
import asyncio
from pathlib import Path
from datetime import datetime, timezone, timedelta

import pytest
from satori import User, Login, Channel, ChannelType, MessageObject
from arclet.entari.config import EntariConfig
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

if not hasattr(EntariConfig, "instance"):
    setattr(
        EntariConfig, "instance", EntariConfig.load(Path(__file__).resolve().parents[1] / "entari.full.example.yml")
    )

from entari_plugin_database import Base

from plugins.llm_chat.reminders import store, service as runtime
from plugins.llm_chat.group_delivery import install_group_delivery
from plugins.llm_chat.tools._delivery import send_with_delivery
from plugins.llm_chat.core.agent_trace import AgentTurnRecorder
from plugins.llm_chat.reminders.runtime import ReminderDecision, propose_reminder_decision

UTC = timezone.utc


@pytest.fixture
async def db(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'runtime.db'}", connect_args={"timeout": 15})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(store, "get_session", factory)
    monkeypatch.setattr(runtime, "migrate_turn_origin_columns", lambda: asyncio.sleep(0))
    try:
        yield factory
    finally:
        await engine.dispose()


class Protocol:
    def __init__(self):
        self.sent = []
        self.dm = []
        self.fail_send = False
        self.fail_dm = False

    async def user_channel_create(self, *, user_id):
        self.dm.append(user_id)
        if self.fail_dm:
            raise TimeoutError("DM channel result unknown")
        return Channel(id="direct-current", type=ChannelType.DIRECT)

    async def send_message(self, channel, message, *args, **kwargs):
        self.sent.append((channel, message))
        if self.fail_send:
            raise TimeoutError("transport result unknown")
        return [MessageObject(id=f"receipt-{len(self.sent)}")]


class Account:
    def __init__(self, connected=False):
        self.platform = "qq"
        self.self_id = "bot"
        self.self_info = Login(platform="qq", user=User(id="bot"))
        self.connected = asyncio.Event()
        if connected:
            self.connected.set()
        self.protocol = Protocol()


async def task(**overrides):
    fields = {
        "operation_id": "one",
        "platform": "qq",
        "account_id": "bot",
        "unified_user_id": "u",
        "platform_user_id": "p",
        "source_channel_id": "group",
        "delivery_channel_id": "group",
        "goal": "check in",
        "due_at": datetime.now(UTC) - timedelta(minutes=1),
    }
    fields.update(overrides)
    return await store.create_task(**fields)


@pytest.fixture
def fake_generation(monkeypatch):
    calls = []
    monkeypatch.setattr(runtime, "get_model_config", lambda *_: SimpleNamespace(name="stub-model"))
    monkeypatch.setattr(runtime, "pin_main_model", lambda model: model)

    async def turn_ref(turn_id):
        assert turn_id == 11
        return "turn-test-11"

    monkeypatch.setattr(runtime, "get_agent_turn_ref", turn_ref)

    async def prepare(config, session, identity, **kwargs):
        assert session.event.message is None
        assert kwargs["scheduled_context"]["late_by_seconds"] >= 0
        calls.append((session, kwargs))
        return SimpleNamespace(lifecycle=SimpleNamespace(agent_turn_id=11), agent_events=AgentTurnRecorder())

    monkeypatch.setattr(runtime, "prepare_scheduled_agent_turn", prepare)
    return calls


async def settled(service):
    await service.claim_ready()
    for _ in range(100):
        await asyncio.sleep(0.02)
        if not service._workers:
            return
    raise AssertionError("reminder worker did not settle")


@pytest.mark.asyncio
async def test_offline_account_online_wake_single_claim_and_group_has_no_quote(db, fake_generation):
    dispose = install_group_delivery()
    account = Account()
    goal = await task()

    async def runner(session, ctx, prepared, **kwargs):
        assert ctx["$event"] is session.event
        await send_with_delivery(session, "hello", None)
        propose_reminder_decision(ReminderDecision(goal.task_ref, "completed"))
        return "completed"

    worker = runtime.ReminderService(SimpleNamespace(model="default"), accounts=lambda: [account], runner=runner)
    try:
        await worker.start()
        await worker.claim_ready()
        assert await store.get_occurrence(store.ReminderClaim(goal.task_ref, "none", 0, 1, datetime.now(UTC))) is None
        account.connected.set()
        worker.account_update(account)
        await asyncio.gather(worker.claim_ready(), worker.claim_ready())
        await settled(worker)
        assert len(fake_generation) == len(account.protocol.sent) == 1
        assert account.protocol.sent[0][0] == "group"
        assert "quote" not in str(account.protocol.sent[0][1]).casefold()
        assert (await store.get_task_for_service(goal.task_ref)).status == "completed"
        occurrence = await store.get_occurrence(store.ReminderClaim(goal.task_ref, "none", 0, 1, datetime.now(UTC)))
        assert occurrence.confirmed_count == 1
        assert occurrence.turn_ref == "turn-test-11"
    finally:
        await worker.aclose()
        await dispose()


@pytest.mark.asyncio
async def test_direct_adapter_account_discovered_after_start_without_account_update(db, fake_generation):
    from satori import LoginStatus
    from entari_plugin_server.patch import DirectAdapterServer

    accounts = []
    account = Account()
    account.protocol.server = object.__new__(DirectAdapterServer)
    account.self_info.status = LoginStatus.ONLINE
    due_task = await task()

    async def runner(session, ctx, prepared, **kwargs):
        propose_reminder_decision(ReminderDecision(due_task.task_ref, "completed"))
        return "silent"

    worker = runtime.ReminderService(SimpleNamespace(model="default"), accounts=lambda: accounts, runner=runner)
    try:
        await worker.start()
        accounts.append(account)
        worker.wake()
        await settled(worker)
        assert (await store.get_task_for_service(due_task.task_ref)).status == "completed"
        assert len(fake_generation) == 1

        account.self_info.status = LoginStatus.OFFLINE
        later = await task(operation_id="offline", unified_user_id="v", platform_user_id="v")
        worker.wake()
        await worker.claim_ready()
        assert (await store.get_task_for_service(later.task_ref)).current_occurrence == 0
    finally:
        await worker.aclose()


@pytest.mark.asyncio
async def test_dm_resolved_only_when_authorized_and_never_falls_back(db, fake_generation):
    account = Account(True)
    goal = await task(delivery_target_type="dm", delivery_channel_id="", delivery_user_id="p", dm_authorized=False)
    worker = runtime.ReminderService(SimpleNamespace(model="default"), accounts=lambda: [account])
    try:
        await worker.start()
        await settled(worker)
        assert account.protocol.dm == []
        assert account.protocol.sent == []
        assert (await store.get_task_for_service(goal.task_ref)).status == "paused"
    finally:
        await worker.aclose()


@pytest.mark.asyncio
async def test_dm_resolution_and_unknown_transport_pause_without_replay(db, fake_generation):
    account = Account(True)
    goal = await task(delivery_target_type="dm", delivery_channel_id="", delivery_user_id="p", dm_authorized=True)
    account.protocol.fail_send = True

    async def runner(session, ctx, prepared, **kwargs):
        assert session.channel.id == "direct-current"
        assert session.channel.type == ChannelType.DIRECT
        await send_with_delivery(session, "private", None)
        return "completed"

    worker = runtime.ReminderService(SimpleNamespace(model="default"), accounts=lambda: [account], runner=runner)
    try:
        await worker.start()
        await settled(worker)
        assert account.protocol.dm == ["p"]
        assert [item[0] for item in account.protocol.sent] == ["direct-current"]
        assert (await store.get_task_for_service(goal.task_ref)).status == "paused"
        worker.wake()
        await worker.claim_ready()
        assert len(account.protocol.sent) == 1
    finally:
        await worker.aclose()


@pytest.mark.asyncio
async def test_three_adaptive_occurrences_including_silent_renewal(db, fake_generation):
    account = Account(True)
    goal = await task(mode="adaptive")
    states = []

    async def runner(session, ctx, prepared, **kwargs):
        sequence = len(states) + 1
        states.append(sequence)
        if sequence != 2:
            await send_with_delivery(session, f"wake-{sequence}", None)
        if sequence != 3:
            propose_reminder_decision(
                ReminderDecision(goal.task_ref, "active", datetime.now(UTC) + timedelta(seconds=1))
            )
        else:
            propose_reminder_decision(ReminderDecision(goal.task_ref, "completed"))
        return "silent" if sequence == 2 else "completed"

    worker = runtime.ReminderService(SimpleNamespace(model="default"), accounts=lambda: [account], runner=runner)
    try:
        await worker.start()
        await settled(worker)
        for _ in range(2):
            await asyncio.sleep(1.05)
            worker.wake()
            await settled(worker)
        assert states == [1, 2, 3]
        assert len(account.protocol.sent) == 2
        assert (await store.get_task_for_service(goal.task_ref)).status == "completed"
    finally:
        await worker.aclose()


@pytest.mark.asyncio
async def test_shutdown_waits_for_cancelled_worker_and_new_service_can_claim(db, fake_generation):
    account = Account(True)
    goal = await task()
    entered = asyncio.Event()

    async def runner(session, ctx, prepared, **kwargs):
        entered.set()
        await asyncio.Event().wait()
        return "completed"

    first = runtime.ReminderService(SimpleNamespace(model="default"), accounts=lambda: [account], runner=runner)
    await first.start()
    await asyncio.wait_for(entered.wait(), 5)
    await first.aclose()
    assert not first._workers
    assert (await store.get_task_for_service(goal.task_ref)).status == "paused"
    other = await task(operation_id="other", unified_user_id="v", platform_user_id="q")
    second = runtime.ReminderService(
        SimpleNamespace(model="default"),
        accounts=lambda: [account],
        runner=lambda *a, **k: asyncio.sleep(0, result="failed"),
    )
    try:
        await second.start()
        await settled(second)
        assert (await store.get_task_for_service(other.task_ref)).current_occurrence == 1
    finally:
        await second.aclose()


@pytest.mark.asyncio
async def test_pre_model_lease_retries_without_catch_up_burst(db, fake_generation):
    account = Account(True)
    goal = await task()
    stale = await store.try_claim_due(goal.task_ref, lease_seconds=1)
    assert stale is not None
    await asyncio.sleep(1.05)
    calls = []

    async def runner(session, ctx, prepared, **kwargs):
        calls.append(kwargs["model_name"])
        propose_reminder_decision(ReminderDecision(goal.task_ref, "completed"))
        return "silent"

    worker = runtime.ReminderService(SimpleNamespace(model="default"), accounts=lambda: [account], runner=runner)
    try:
        await worker.start()
        await settled(worker)
        current = await store.get_task_for_service(goal.task_ref)
        assert current.current_occurrence == 2
        assert current.status == "completed"
        assert calls == ["stub-model"]
        recovered = await store.get_occurrence(stale)
        assert recovered.state == "expired_recovered"
    finally:
        await worker.aclose()


@pytest.mark.asyncio
async def test_confirmed_output_without_resolve_pauses_instead_of_completing(db, fake_generation):
    account = Account(True)
    goal = await task()

    async def runner(session, ctx, prepared, **kwargs):
        await send_with_delivery(session, "sent", None)
        return "completed"

    worker = runtime.ReminderService(SimpleNamespace(model="default"), accounts=lambda: [account], runner=runner)
    try:
        await worker.start()
        await settled(worker)
        current = await store.get_task_for_service(goal.task_ref)
        occurrence = await store.get_occurrence(store.ReminderClaim(goal.task_ref, "", 0, 1, datetime.now(UTC)))
        assert current.status == "paused"
        assert current.current_occurrence == 1
        assert occurrence.confirmed_count == 1
        assert occurrence.failure_reason == "missing_reminder_decision"
        await worker.claim_ready()
        assert len(account.protocol.sent) == 1
    finally:
        await worker.aclose()


@pytest.mark.asyncio
async def test_dm_channel_resolution_unknown_never_sends_group(db, fake_generation):
    account = Account(True)
    account.protocol.fail_dm = True
    goal = await task(delivery_target_type="dm", delivery_channel_id="", delivery_user_id="p", dm_authorized=True)
    worker = runtime.ReminderService(SimpleNamespace(model="default"), accounts=lambda: [account])
    try:
        await worker.start()
        await settled(worker)
        assert account.protocol.dm == ["p"]
        assert account.protocol.sent == []
        assert (await store.get_task_for_service(goal.task_ref)).status == "paused"
        occurrence = await store.get_occurrence(store.ReminderClaim(goal.task_ref, "", 0, 1, datetime.now(UTC)))
        assert occurrence.dm_started
        assert occurrence.failure_reason == "dm_target_unknown"
    finally:
        await worker.aclose()


@pytest.mark.asyncio
async def test_two_users_in_one_channel_run_concurrently(db, fake_generation):
    account = Account(True)
    first = await task()
    second = await task(operation_id="two", unified_user_id="other", platform_user_id="other-platform")
    both_started = asyncio.Event()
    started = set()

    async def runner(session, ctx, prepared, **kwargs):
        started.add(session.user.id)
        if len(started) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), 2)
        propose_reminder_decision(
            ReminderDecision(first.task_ref if session.user.id == "p" else second.task_ref, "completed")
        )
        return "silent"

    worker = runtime.ReminderService(
        SimpleNamespace(model="default"), accounts=lambda: [account], runner=runner, concurrency=2
    )
    try:
        await worker.start()
        await settled(worker)
        assert started == {"p", "other-platform"}
        assert (await store.get_task_for_service(first.task_ref)).status == "completed"
        assert (await store.get_task_for_service(second.task_ref)).status == "completed"
    finally:
        await worker.aclose()


@pytest.mark.asyncio
async def test_migration_precedes_any_existing_database_query(db, fake_generation, monkeypatch):
    sequence = []
    migrate = runtime.migrate_turn_origin_columns
    query = runtime.recover_stale

    async def traced_migration():
        sequence.append("migration")
        await migrate()

    async def traced_recovery():
        sequence.append("query")
        await query()

    monkeypatch.setattr(runtime, "migrate_turn_origin_columns", traced_migration)
    monkeypatch.setattr(runtime, "recover_stale", traced_recovery)
    worker = runtime.ReminderService(SimpleNamespace(model="default"), accounts=lambda: [])
    try:
        await worker.start()
        assert sequence[:2] == ["migration", "query"]
    finally:
        await worker.aclose()


@pytest.mark.asyncio
async def test_cancel_during_transport_keeps_real_receipt_but_cannot_reactivate(db, fake_generation):
    account = Account(True)
    goal = await task()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def delayed_send(channel, message, *args, **kwargs):
        entered.set()
        await release.wait()
        return [MessageObject(id="real-after-cancel")]

    account.protocol.send_message = delayed_send

    async def runner(session, ctx, prepared, **kwargs):
        await send_with_delivery(session, "started", None)
        propose_reminder_decision(ReminderDecision(goal.task_ref, "completed"))
        return "completed"

    worker = runtime.ReminderService(SimpleNamespace(model="default"), accounts=lambda: [account], runner=runner)
    try:
        await worker.start()
        await asyncio.wait_for(entered.wait(), 5)
        current = await store.get_task_for_service(goal.task_ref)
        await store.cancel_task(
            goal.task_ref,
            operation_id="cancel-mid-transport",
            expected_revision=current.revision,
            account_id="bot",
            unified_user_id="u",
        )
        release.set()
        await settled(worker)
        current = await store.get_task_for_service(goal.task_ref)
        occurrence = await store.get_occurrence(store.ReminderClaim(goal.task_ref, "", 0, 1, datetime.now(UTC)))
        assert current.status == "cancelled"
        assert occurrence.state == "cancelled"
        assert occurrence.confirmed_count == 1
        assert "real-after-cancel" in occurrence.confirmed_receipts_json
    finally:
        release.set()
        await worker.aclose()


@pytest.mark.asyncio
async def test_expired_offline_task_completes_without_model_or_catch_up(db, fake_generation):
    account = Account()
    expired = await task(due_at=datetime.now(UTC) - timedelta(days=5), expires_at=datetime.now(UTC) - timedelta(days=4))
    worker = runtime.ReminderService(SimpleNamespace(model="default"), accounts=lambda: [account])
    try:
        await worker.start()
        await worker.claim_ready()
        assert (await store.get_task_for_service(expired.task_ref)).status == "completed"
        assert (await store.get_task_for_service(expired.task_ref)).current_occurrence == 0
        assert fake_generation == []
        assert account.protocol.sent == []
    finally:
        await worker.aclose()


@pytest.mark.asyncio
async def test_reschedule_revokes_running_worker_without_late_send(db, fake_generation):
    account = Account(True)
    goal = await task()
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def runner(session, ctx, prepared, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    worker = runtime.ReminderService(SimpleNamespace(model="default"), accounts=lambda: [account], runner=runner)
    try:
        await worker.start()
        await asyncio.wait_for(entered.wait(), 5)
        current = await store.get_task_for_service(goal.task_ref)
        future = datetime.now(UTC) + timedelta(hours=2)
        await store.update_task(
            goal.task_ref,
            operation_id="move-during-model",
            expected_revision=current.revision,
            account_id="bot",
            unified_user_id="u",
            due_at=future,
        )
        worker.wake()
        await asyncio.wait_for(cancelled.wait(), 5)
        await settled(worker)
        current = await store.get_task_for_service(goal.task_ref)
        occurrence = await store.get_occurrence(store.ReminderClaim(goal.task_ref, "", 0, 1, datetime.now(UTC)))
        assert current.status == "active"
        assert current.current_occurrence == 1
        assert current.due_at == future.replace(tzinfo=None)
        assert occurrence.state == "paused"
        assert occurrence.failure_reason == "task_updated"
        assert account.protocol.sent == []
    finally:
        await worker.aclose()


@pytest.mark.asyncio
async def test_active_task_cap_applies_to_create_and_resume(db):
    created = [await task(operation_id=f"cap-{number}", goal=f"task-{number}") for number in range(20)]
    with pytest.raises(store.ReminderConflict, match="20 active"):
        await task(operation_id="cap-overflow")
    paused = await store.pause_task(
        created[0].task_ref, operation_id="pause-for-slot", expected_revision=0, account_id="bot", unified_user_id="u"
    )
    replacement = await task(operation_id="cap-slot")
    assert replacement.status == "active"
    with pytest.raises(store.ReminderConflict, match="20 active"):
        await store.resume_task(
            paused.task_ref,
            operation_id="resume-overflow",
            expected_revision=paused.revision,
            account_id="bot",
            unified_user_id="u",
        )


@pytest.mark.asyncio
async def test_disabled_autonomous_contact_does_not_block_explicit_reminder(db, fake_generation):
    account = Account(True)
    autonomous = await task(origin="autonomous")
    explicit = await task(operation_id="explicit", goal="delegated reminder")
    await store.set_preference(operation_id="disable-contact", account_id="bot", unified_user_id="u", enabled=False)

    async def runner(session, ctx, prepared, **kwargs):
        from plugins.llm_chat.reminders.runtime import propose_reminder_decision

        propose_reminder_decision(ReminderDecision(explicit.task_ref, "completed"))
        return "silent"

    worker = runtime.ReminderService(SimpleNamespace(model="default"), accounts=lambda: [account], runner=runner)
    try:
        await worker.start()
        await settled(worker)
        assert (await store.get_task_for_service(autonomous.task_ref)).current_occurrence == 0
        assert (await store.get_task_for_service(explicit.task_ref)).status == "completed"
    finally:
        await worker.aclose()
