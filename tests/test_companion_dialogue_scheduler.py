"""Native Session/SQLite regressions for the single companion dialogue owner."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from satori import ChannelType
from companion_helpers import NOW, Clock, scope, until, config, native_rows, completed_turn
from arclet.letoderea.context import Contexts

from plugins.llm_chat.models import ChatScope, ScopePersonaSelection
from plugins.llm_chat.companion import store, dialogue_store
from utils.companion_core.dialogue import DialogueDecision, ambient_deadline, decision_deadlines
from plugins.llm_chat.companion.persistence import transaction
from plugins.llm_chat.companion.dialogue_models import CompanionDialogue
from plugins.llm_chat.companion.dialogue_manager import DialogueManager, DialogueOutcome

pytest_plugins = ["companion_helpers"]


async def enable(database, *, channel="companion-group"):
    native_scope = await scope(database, channel=channel)
    await store.set_mode(native_scope.id, "companion", persona_key="alpha", now=NOW)
    return native_scope


async def checkpoint(manager):
    """Let the real async scheduler service a wake, without advancing its clock."""
    manager.wake()
    await asyncio.sleep(0.03)


def test_decision_validation_and_finite_alarm_semantics():
    assert DialogueDecision() == DialogueDecision("observe", 30)
    for action, seconds in (("poll", 30), ("wait", True), ("observe", 0), ("wait", 3601), ("end", 1.5)):
        with pytest.raises(ValueError, match="(?i)companion dialogue"):
            DialogueDecision(action, seconds)
    assert decision_deadlines(DialogueDecision("observe", 30), NOW) == (NOW + timedelta(seconds=30), None)
    assert decision_deadlines(DialogueDecision("wait", 60), NOW) == (NOW + timedelta(seconds=60),) * 2
    assert decision_deadlines(DialogueDecision("end"), NOW) == (None, None)
    assert ambient_deadline(NOW, NOW + timedelta(seconds=4)) == NOW + timedelta(seconds=5)


@pytest.mark.asyncio
async def test_normal_resolution_is_read_only_and_never_runs(companion_db, native_transport):
    calls = []

    async def runner(batch):
        calls.append(batch)
        return DialogueOutcome()

    manager = DialogueManager(config(), runner, session_factory=companion_db.factory, clock=Clock())
    try:
        assert not await manager.admit(native_transport.session(), Contexts(), directed=True)
        assert await native_rows(companion_db, ChatScope) == []
        native_scope = await scope(companion_db)
        assert not await manager.admit(native_transport.session(), Contexts(), directed=False)
        assert not manager.active(native_scope.id)
        assert await native_rows(companion_db, CompanionDialogue) == []
        assert calls == []
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_ambient_two_second_quiet_batch_keeps_real_member_sessions(companion_db, native_transport):
    native_scope = await enable(companion_db)
    clock, calls = Clock(), []

    async def runner(batch):
        calls.append(batch)
        return DialogueOutcome(DialogueDecision("end"))

    manager = DialogueManager(config(), runner, clock=clock, session_factory=companion_db.factory)
    alice = native_transport.session(user="platform-alice", message="alice-ambient")
    bob = native_transport.session(user="platform-bob", message="bob-ambient")
    alice_ctx, bob_ctx = Contexts(), Contexts()
    try:
        assert await manager.admit(alice, alice_ctx, directed=False)
        clock.advance(seconds=1)
        assert await manager.admit(bob, bob_ctx, directed=False)
        clock.advance(seconds=1)
        await checkpoint(manager)
        assert calls == []
        clock.advance(seconds=1)
        manager.wake()
        await until(lambda: len(calls) == 1 and not manager.active(native_scope.id))
        batch = calls[0]
        assert [item.session for item in batch.inputs] == [alice, bob]
        assert [item.ctx for item in batch.inputs] == [alice_ctx, bob_ctx]
        assert [item.session.user.id for item in batch.inputs] == ["platform-alice", "platform-bob"]
        assert batch.scope.id == native_scope.id
        assert batch.mode.mode == "companion"
        assert batch.account is native_transport.account
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_continuous_ambient_burst_cannot_move_past_five_second_cap(companion_db, native_transport):
    native_scope = await enable(companion_db)
    clock, calls = Clock(), []

    async def runner(batch):
        calls.append(batch)
        return DialogueOutcome(DialogueDecision("end"))

    manager = DialogueManager(config(), runner, clock=clock, session_factory=companion_db.factory)
    try:
        for second in range(5):
            if second:
                clock.advance(seconds=1)
            await manager.admit(native_transport.session(message=f"burst-{second}"), Contexts(), directed=False)
            await checkpoint(manager)
            assert calls == []
        clock.advance(seconds=1)
        manager.wake()
        await until(lambda: len(calls) == 1 and not manager.active(native_scope.id))
        assert len(calls[0].inputs) == 5
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_live_push_direct_priority_and_bounded_overflow_never_duplicate(companion_db, native_transport):
    native_scope = await enable(companion_db)
    clock = Clock()
    entered, release = asyncio.Event(), asyncio.Event()
    batches, accepted, seen = [], [], []
    running = 0
    peak = 0

    async def runner(batch):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        batches.append(batch)
        seen.extend(item.session.event.message.id for item in batch.inputs)
        try:
            if len(batches) == 1:

                def admitter(item):
                    accepted.append(item)
                    seen.append(item.session.event.message.id)
                    return True

                batch.bind_admitter(admitter)
                entered.set()
                await release.wait()
            return DialogueOutcome(DialogueDecision("end"))
        finally:
            running -= 1

    manager = DialogueManager(
        config(),
        runner,
        clock=clock,
        session_factory=companion_db.factory,
        max_pending_inputs=2,
        max_batch_inputs=1,
        max_live_inputs=1,
    )
    overflow = None
    try:
        await manager.admit(native_transport.session(message="root"), Contexts(), directed=True)
        await asyncio.wait_for(entered.wait(), 5)
        bob = native_transport.session(user="platform-bob", message="live")
        await manager.admit(bob, Contexts(), directed=False)
        assert [item.session for item in accepted] == [bob]
        await manager.admit(native_transport.session(message="queued-ambient"), Contexts(), directed=False)
        await manager.admit(native_transport.session(message="queued-direct"), Contexts(), directed=True)
        overflow = asyncio.create_task(
            manager.admit(
                native_transport.session(message="backpressured"),
                Contexts(),
                directed=False,
            )
        )
        await asyncio.sleep(0.03)
        assert not overflow.done()
        assert len(batches) == 1
        release.set()
        assert await asyncio.wait_for(overflow, 5)
        clock.advance(seconds=5)
        manager.wake()
        await until(lambda: len(seen) == 5 and not manager.active(native_scope.id))
        assert peak == 1
        assert seen == ["root", "live", "queued-direct", "queued-ambient", "backpressured"]
        assert len(set(seen)) == 5
        assert await batches[0].admit_live() == ()
        assert not await batches[0].valid()
    finally:
        release.set()
        await manager.aclose()
        if overflow is not None:
            await asyncio.gather(overflow, return_exceptions=True)


@pytest.mark.asyncio
async def test_observe_is_unread_gate_not_an_idle_model_poll(companion_db, native_transport):
    native_scope = await enable(companion_db)
    clock, calls = Clock(), []

    async def runner(batch):
        calls.append(batch)
        return DialogueOutcome(DialogueDecision("observe", 30))

    manager = DialogueManager(config(), runner, clock=clock, session_factory=companion_db.factory)
    try:
        await manager.admit(native_transport.session(message="first"), Contexts(), directed=True)
        await until(lambda: len(calls) == 1 and not manager.active(native_scope.id))
        clock.advance(seconds=1)
        await manager.admit(native_transport.session(message="unread"), Contexts(), directed=False)
        clock.advance(seconds=10)
        await checkpoint(manager)
        assert len(calls) == 1
        clock.advance(seconds=19)
        manager.wake()
        await until(lambda: len(calls) == 2 and not manager.active(native_scope.id))
        assert calls[1].inputs[0].session.event.message.id == "unread"
        for _ in range(3):
            clock.advance(hours=1)
            await checkpoint(manager)
        assert len(calls) == 2
        rows = await native_rows(companion_db, CompanionDialogue)
        assert rows[0].action == "observe"
        assert rows[0].wake_at is None
    finally:
        await manager.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["observe", "wait"])
async def test_direct_input_wakes_observation_or_wait_early(companion_db, native_transport, action):
    native_scope = await enable(companion_db)
    clock, calls = Clock(), []

    async def runner(batch):
        calls.append(batch)
        return DialogueOutcome(DialogueDecision(action, 60) if len(calls) == 1 else DialogueDecision("end"))

    manager = DialogueManager(config(), runner, clock=clock, session_factory=companion_db.factory)
    try:
        await manager.admit(native_transport.session(message="initial"), Contexts(), directed=True)
        await until(lambda: len(calls) == 1 and not manager.active(native_scope.id))
        clock.advance(seconds=1)
        await manager.admit(native_transport.session(message="direct-wakeup"), Contexts(), directed=True)
        await until(lambda: len(calls) == 2 and not manager.active(native_scope.id))
        assert calls[1].trigger == "input"
        clock.advance(minutes=5)
        await checkpoint(manager)
        assert len(calls) == 2
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_explicit_wait_restores_once_without_replaying_old_user_input(companion_db, native_transport):
    native_scope = await enable(companion_db)
    clock, calls, restored = Clock(), [], []
    anchor = native_transport.session(message="real-before-restart")

    async def first_runner(batch):
        calls.append(batch)
        return DialogueOutcome(DialogueDecision("wait", 60), confirmed_deliveries=1)

    first = DialogueManager(config(), first_runner, clock=clock, session_factory=companion_db.factory)
    await first.admit(anchor, Contexts(), directed=True)
    await until(lambda: len(calls) == 1 and not first.active(native_scope.id))
    await first.aclose()

    async def restore(native_scope):
        restored.append(native_scope.id)
        return anchor

    async def resumed_runner(batch):
        calls.append(batch)
        assert batch.trigger == "wait"
        assert batch.inputs == ()
        return DialogueOutcome(DialogueDecision("end"))

    resumed = DialogueManager(
        config(),
        resumed_runner,
        clock=clock,
        session_factory=companion_db.factory,
        restore_session=restore,
    )
    try:
        await resumed.start()
        clock.advance(seconds=59)
        await checkpoint(resumed)
        assert len(calls) == 1
        assert restored == []
        clock.advance(seconds=1)
        resumed.wake()
        await until(lambda: len(calls) == 2 and not resumed.active(native_scope.id))
        clock.advance(hours=1)
        await checkpoint(resumed)
        assert len(calls) == 2
        assert restored == [native_scope.id]
        rows = await native_rows(companion_db, CompanionDialogue)
        assert rows[0].wake_at is None
        assert rows[0].confirmed_deliveries == 1
    finally:
        await resumed.aclose()


@pytest.mark.asyncio
async def test_begun_and_unknown_work_have_no_automatic_replay(companion_db, native_transport):
    native_scope = await enable(companion_db)
    mode = await store.get_mode(native_scope.id)
    await dialogue_store.initialize_store(now=NOW, session_factory=companion_db.factory)
    assert await dialogue_store.begin(mode, now=NOW, session_factory=companion_db.factory)
    calls, clock = [], Clock()

    async def runner(batch):
        calls.append(batch)
        return DialogueOutcome(DialogueDecision("wait", 1), status="unknown")

    manager = DialogueManager(config(), runner, clock=clock, session_factory=companion_db.factory)
    try:
        await manager.start()
        clock.advance(hours=1)
        await checkpoint(manager)
        assert calls == []
        assert (await native_rows(companion_db, CompanionDialogue))[0].status == "unknown"
        await manager.admit(native_transport.session(message="new-not-replay"), Contexts(), directed=True)
        await until(lambda: len(calls) == 1 and not manager.active(native_scope.id))
        clock.advance(hours=1)
        await checkpoint(manager)
        assert len(calls) == 1
        row = (await native_rows(companion_db, CompanionDialogue))[0]
        assert row.status == "unknown"
        assert row.wake_at is None
        assert row.action == "end"
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_mode_and_persona_changes_fence_old_batches_and_restored_waits(companion_db, native_transport):
    native_scope = await enable(companion_db)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def runner(batch):
        calls.append(batch)
        entered.set()
        await release.wait()
        return DialogueOutcome(DialogueDecision("wait", 30))

    manager = DialogueManager(config(), runner, clock=Clock(), session_factory=companion_db.factory)
    try:
        await manager.admit(native_transport.session(), Contexts(), directed=True)
        await asyncio.wait_for(entered.wait(), 5)
        async with companion_db.factory() as db, db.begin():
            selection = await db.get(ScopePersonaSelection, native_scope.id)
            selection.persona_key = "beta"
        assert not await calls[0].valid()
        assert not await manager.admit(native_transport.session(message="stale-persona"), Contexts(), directed=True)
        await manager.cancel_scope(native_scope.id)
        release.set()
        await until(lambda: not manager.active(native_scope.id))
        assert await calls[0].admit_live() == ()
        with pytest.raises(RuntimeError):
            calls[0].bind_admitter(lambda _item: True)
    finally:
        release.set()
        await manager.aclose()
    resumed_calls = []

    async def resumed_runner(batch):
        resumed_calls.append(batch)
        return DialogueOutcome()

    resumed = DialogueManager(config(), resumed_runner, clock=Clock(), session_factory=companion_db.factory)
    try:
        await resumed.start()
        await checkpoint(resumed)
        assert resumed_calls == []
        row = (await native_rows(companion_db, CompanionDialogue))[0]
        assert row.status == "cancelled"
        assert row.wake_at is None
    finally:
        await resumed.aclose()


@pytest.mark.asyncio
async def test_channels_and_private_peers_have_independent_owners(companion_db, native_transport):
    group = await enable(companion_db)
    private = await enable(companion_db, channel="private:platform-bob")
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def runner(batch):
        calls.append(batch)
        if len(calls) == 2:
            entered.set()
        await release.wait()
        return DialogueOutcome(DialogueDecision("end"))

    manager = DialogueManager(config(), runner, clock=Clock(), session_factory=companion_db.factory)
    try:
        await manager.admit(native_transport.session(), Contexts(), directed=True)
        await manager.admit(
            native_transport.session(
                channel="private:platform-bob",
                user="platform-bob",
                channel_type=ChannelType.DIRECT,
            ),
            Contexts(),
            directed=True,
        )
        await asyncio.wait_for(entered.wait(), 5)
        assert {batch.scope.id for batch in calls} == {group.id, private.id}
        assert {batch.inputs[0].session.user.id for batch in calls} == {"platform-alice", "platform-bob"}
        assert manager.active(group.id)
        assert manager.active(private.id)
    finally:
        release.set()
        await manager.aclose()


@pytest.mark.asyncio
async def test_opportunity_uses_same_owner_and_queues_live_input_without_replaying(companion_db, native_transport):
    native_scope = await enable(companion_db)
    clock, calls = Clock(), []
    entered, release = asyncio.Event(), asyncio.Event()
    opportunity = {"topic": "The colors of an autumn evening", "source": "committed-reflection"}

    async def runner(batch):
        calls.append(batch)
        if batch.trigger == "opportunity":
            assert batch.inputs == ()
            assert batch.opportunity is opportunity
            entered.set()
            await release.wait()
        return DialogueOutcome(DialogueDecision("end"))

    manager = DialogueManager(config(), runner, clock=clock, session_factory=companion_db.factory)
    try:
        assert await manager.offer_opportunity(
            native_scope.id,
            opportunity,
            session=native_transport.session(),
            key="proposal-1",
        )
        await asyncio.wait_for(entered.wait(), 5)
        assert not await manager.offer_opportunity(native_scope.id, opportunity, key="proposal-1")
        assert not await manager.offer_opportunity(native_scope.id, {"topic": "Another"}, key="proposal-2")
        assert await manager.admit(native_transport.session(message="during-opportunity"), Contexts(), directed=True)
        assert len(calls) == 1
        release.set()
        await until(lambda: len(calls) == 2 and not manager.active(native_scope.id))
        assert calls[1].trigger == "input"
        assert calls[1].inputs[0].session.event.message.id == "during-opportunity"
        assert not await manager.offer_opportunity(native_scope.id, opportunity, key="proposal-1")
    finally:
        release.set()
        await manager.aclose()


@pytest.mark.asyncio
async def test_shutdown_fences_inherited_callbacks_and_does_not_restore_begun_work(companion_db, native_transport):
    await enable(companion_db)
    entered = asyncio.Event()
    calls = []

    async def runner(batch):
        calls.append(batch)
        batch.bind_admitter(lambda _item: True)
        entered.set()
        await asyncio.Event().wait()
        return DialogueOutcome()

    manager = DialogueManager(config(), runner, clock=Clock(), session_factory=companion_db.factory)
    await manager.admit(native_transport.session(), Contexts(), directed=True)
    await asyncio.wait_for(entered.wait(), 5)
    await manager.aclose()
    assert not await calls[0].valid()
    assert await calls[0].admit_live() == ()
    with pytest.raises(RuntimeError):
        calls[0].bind_admitter(lambda _item: True)
    assert not await manager.admit(native_transport.session(message="after-close"), Contexts(), directed=True)
    row = (await native_rows(companion_db, CompanionDialogue))[0]
    assert row.status == "cancelled"
    assert row.run_token is None
    assert row.wake_at is None


@pytest.mark.asyncio
async def test_native_begin_transaction_claims_scope_only_once(companion_db):
    native_scope = await enable(companion_db)
    mode = await store.get_mode(native_scope.id)
    await dialogue_store.initialize_store(now=NOW, session_factory=companion_db.factory)
    tokens = await asyncio.gather(
        *(dialogue_store.begin(mode, now=NOW, session_factory=companion_db.factory) for _ in range(2))
    )
    assert sum(token is not None for token in tokens) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["mode", "persona"])
async def test_restoration_invalidates_persisted_alarm_without_explicit_cancel(
    companion_db,
    native_transport,
    change,
):
    native_scope = await enable(companion_db)
    clock = Clock()

    async def runner(batch):
        return DialogueOutcome(DialogueDecision("wait", 60))

    manager = DialogueManager(config(), runner, clock=clock, session_factory=companion_db.factory)
    await manager.admit(native_transport.session(), Contexts(), directed=True)
    await until(lambda: not manager.active(native_scope.id))
    await manager.aclose()
    if change == "mode":
        await store.set_mode(native_scope.id, "normal", persona_key="alpha", now=NOW)
    else:
        async with companion_db.factory() as db, db.begin():
            selection = await db.get(ScopePersonaSelection, native_scope.id)
            selection.persona_key = "beta"
    called = []

    async def restore(native_scope):
        called.append(native_scope.id)
        return native_transport.session()

    resumed = DialogueManager(
        config(),
        runner,
        clock=clock,
        session_factory=companion_db.factory,
        restore_session=restore,
    )
    try:
        await resumed.start()
        clock.advance(hours=1)
        await checkpoint(resumed)
        assert called == []
        row = (await native_rows(companion_db, CompanionDialogue))[0]
        assert row.status == "cancelled"
        assert row.wake_at is None
    finally:
        await resumed.aclose()


@pytest.mark.asyncio
async def test_offline_wait_keeps_one_intent_until_transport_is_available(companion_db, native_transport):
    native_scope = await enable(companion_db)
    clock = Clock()

    async def initial_runner(batch):
        return DialogueOutcome(DialogueDecision("wait", 1))

    first = DialogueManager(config(), initial_runner, clock=clock, session_factory=companion_db.factory)
    await first.admit(native_transport.session(), Contexts(), directed=True)
    await until(lambda: not first.active(native_scope.id))
    await first.aclose()
    clock.advance(seconds=1)
    available = False
    lookups, calls = [], []

    async def restore(native_scope):
        lookups.append(native_scope.id)
        return native_transport.session() if available else None

    async def resumed_runner(batch):
        calls.append(batch)
        return DialogueOutcome(DialogueDecision("end"))

    resumed = DialogueManager(
        config(),
        resumed_runner,
        clock=clock,
        session_factory=companion_db.factory,
        restore_session=restore,
    )
    try:
        await resumed.start()
        await until(lambda: len(lookups) == 1)
        await asyncio.sleep(0.03)
        assert len(lookups) == 1
        assert calls == []
        assert (await native_rows(companion_db, CompanionDialogue))[0].wake_at is not None
        available = True
        resumed.wake()
        await until(lambda: len(calls) == 1 and not resumed.active(native_scope.id))
        assert calls[0].trigger == "wait"
        assert calls[0].inputs == ()
    finally:
        await resumed.aclose()


@pytest.mark.asyncio
async def test_full_ambient_backlog_keeps_a_reserved_direct_priority_slot(companion_db, native_transport):
    native_scope = await enable(companion_db)
    clock = Clock()
    entered, release = asyncio.Event(), asyncio.Event()
    messages = []

    async def runner(batch):
        messages.extend(item.session.event.message.id for item in batch.inputs)
        if len(messages) == 1:
            entered.set()
            await release.wait()
        return DialogueOutcome(DialogueDecision("end"))

    manager = DialogueManager(
        config(),
        runner,
        clock=clock,
        session_factory=companion_db.factory,
        max_pending_inputs=1,
        max_batch_inputs=1,
    )
    try:
        await manager.admit(native_transport.session(message="root"), Contexts(), directed=True)
        await asyncio.wait_for(entered.wait(), 5)
        await manager.admit(native_transport.session(message="full-ambient"), Contexts(), directed=False)
        assert await asyncio.wait_for(
            manager.admit(
                native_transport.session(message="urgent-direct"),
                Contexts(),
                directed=True,
            ),
            5,
        )
        release.set()
        await until(lambda: len(messages) == 2)
        assert messages == ["root", "urgent-direct"]
        clock.advance(seconds=2)
        manager.wake()
        await until(lambda: len(messages) == 3 and not manager.active(native_scope.id))
        assert messages == ["root", "urgent-direct", "full-ambient"]
    finally:
        release.set()
        await manager.aclose()


@pytest.mark.asyncio
async def test_wait_and_new_opportunity_refresh_context_after_real_chat(companion_db, native_transport):
    native_scope = await enable(companion_db)
    clock, calls = Clock(), []

    async def record_feedback():
        turn = await completed_turn(companion_db, native_scope, at=clock())
        member = await store.record_chat(
            native_scope.id,
            "alpha",
            user_id="alice-unified",
            platform_user_id="platform-alice",
            display_name="Alice",
            turn_id=turn.id,
            now=clock(),
        )
        assert member is not None

    async def runner(batch):
        calls.append(batch)
        assert batch.mode == await store.get_mode(native_scope.id)
        if len(calls) == 1:
            await record_feedback()
            return DialogueOutcome(DialogueDecision("wait", 1))
        return DialogueOutcome(DialogueDecision("end"))

    manager = DialogueManager(config(), runner, clock=clock, session_factory=companion_db.factory)
    try:
        await manager.admit(native_transport.session(message="live-root"), Contexts(), directed=True)
        await until(lambda: len(calls) == 1 and not manager.active(native_scope.id))
        updated = await store.get_mode(native_scope.id)
        assert updated.context_revision > calls[0].mode.context_revision
        clock.advance(seconds=1)
        manager.wake()
        await until(lambda: len(calls) == 2 and not manager.active(native_scope.id))
        assert calls[1].trigger == "wait"
        assert calls[1].mode == updated
        await record_feedback()
        updated = await store.get_mode(native_scope.id)
        assert await manager.offer_opportunity(native_scope.id, {"topic": "newly committed"}, key="new-context")
        await until(lambda: len(calls) == 3 and not manager.active(native_scope.id))
        assert calls[2].trigger == "opportunity"
        assert calls[2].mode == updated
    finally:
        await manager.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["wait", "opportunity"])
async def test_retained_session_cannot_consume_offline_no_input_intent(
    companion_db,
    native_transport,
    trigger,
):
    native_scope = await enable(companion_db)
    clock, calls, lookups = Clock(), [], []

    async def restore(current_scope):
        lookups.append(current_scope.id)
        return native_transport.session(message="fresh-host") if native_transport.account.connected.is_set() else None

    async def runner(batch):
        calls.append(batch)
        return DialogueOutcome(
            DialogueDecision("wait", 1) if len(calls) == 1 and trigger == "wait" else DialogueDecision("end")
        )

    manager = DialogueManager(
        config(),
        runner,
        clock=clock,
        session_factory=companion_db.factory,
        restore_session=restore,
    )
    try:
        await manager.admit(native_transport.session(message="retained-root"), Contexts(), directed=True)
        await until(lambda: len(calls) == 1 and not manager.active(native_scope.id))
        native_transport.account.connected.clear()
        if trigger == "opportunity":
            assert await manager.offer_opportunity(native_scope.id, {"topic": "retained"}, key="offline-topic")
        clock.advance(seconds=1)
        manager.wake()
        await until(lambda: manager._owners[native_scope.id].transport_blocked)
        row = (await native_rows(companion_db, CompanionDialogue))[0]
        assert row.run_token is None
        assert row.status != "running"
        assert len(calls) == 1
        assert lookups == [native_scope.id]
        if trigger == "wait":
            assert row.wake_at is not None
        else:
            assert manager._owners[native_scope.id].opportunity is not None
        native_transport.account.connected.set()
        manager.wake()
        await until(lambda: len(calls) == 2 and not manager.active(native_scope.id))
        assert calls[1].trigger == trigger
        assert calls[1].inputs == ()
        assert calls[1].session.event.message.id == "fresh-host"
    finally:
        native_transport.account.connected.set()
        await manager.aclose()


@pytest.mark.asyncio
async def test_fitting_live_direct_input_bypasses_oversized_ambient_head(companion_db, native_transport):
    native_scope = await enable(companion_db)
    clock, batches, accepted = Clock(), [], []
    entered, release = asyncio.Event(), asyncio.Event()

    async def runner(batch):
        batches.append(batch)
        if len(batches) == 1:

            def admitter(item):
                accepted.append(item)
                return True

            batch.bind_admitter(admitter)
            entered.set()
            await release.wait()
        return DialogueOutcome(DialogueDecision("end"))

    manager = DialogueManager(
        config(),
        runner,
        clock=clock,
        session_factory=companion_db.factory,
        max_batch_chars=8,
    )
    try:
        await manager.admit(native_transport.session(message="root", content="root"), Contexts(), directed=True)
        await asyncio.wait_for(entered.wait(), 5)
        ambient = native_transport.session(message="oversized-ambient", content="a" * 9)
        urgent = native_transport.session(message="urgent", content="yes")
        await manager.admit(ambient, Contexts(), directed=False)
        assert accepted == []
        await manager.admit(urgent, Contexts(), directed=True)
        assert [item.session for item in accepted] == [urgent]
        assert [item.session for item in manager._owners[native_scope.id].pending] == [ambient]
        release.set()
        clock.advance(seconds=2)
        manager.wake()
        await until(lambda: len(batches) == 2 and not manager.active(native_scope.id))
        assert [item.session for item in batches[1].inputs] == [ambient]
    finally:
        release.set()
        await manager.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("close", [False, True])
async def test_cancellation_before_execute_first_step_settles_claim(companion_db, native_transport, close):
    native_scope = await enable(companion_db)
    calls = []

    async def runner(batch):
        calls.append(batch)
        return DialogueOutcome(DialogueDecision("wait", 1))

    manager = DialogueManager(config(), runner, clock=Clock(), session_factory=companion_db.factory)
    try:
        await manager.admit(native_transport.session(message="not-started"), Contexts(), directed=False)
        owner = manager._owners[native_scope.id]
        # The ambient clock is not due, so only this explicit launch can claim it.
        # _launch returns before the newly created execution task's first step.
        await manager._launch(owner)
        task = owner.task
        assert task is not None
        assert not task.done()
        assert not owner.executing
        if close:
            await manager.aclose()
        else:
            await manager.cancel_scope(native_scope.id)
            await asyncio.wait_for(task, 5)
        row = (await native_rows(companion_db, CompanionDialogue))[0]
        assert calls == []
        assert row.status == "cancelled"
        assert row.run_token is None
        assert row.wake_at is None
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_cancel_during_sqlite_locked_finish_drains_owned_settlement(
    companion_db,
    native_transport,
    monkeypatch,
):
    native_scope = await enable(companion_db)
    entered, release, finishing = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_finish = dialogue_store.finish

    async def held_finish(*args, **kwargs):
        finishing.set()
        return await original_finish(*args, **kwargs)

    monkeypatch.setattr(dialogue_store, "finish", held_finish)

    async def runner(batch):
        receipts = await batch.session.send("A confirmed scheduler receipt")
        entered.set()
        await release.wait()
        return DialogueOutcome(DialogueDecision("wait", 1), confirmed_deliveries=len(receipts))

    manager = DialogueManager(config(), runner, clock=Clock(), session_factory=companion_db.factory)
    try:
        await manager.admit(native_transport.session(), Contexts(), directed=True)
        await asyncio.wait_for(entered.wait(), 5)
        task = manager._owners[native_scope.id].task
        assert task is not None
        async with transaction(companion_db.factory) as db:
            release.set()
            await asyncio.wait_for(finishing.wait(), 5)
            # This must return without a competing write against our held lock.
            await asyncio.wait_for(manager.cancel_scope(native_scope.id), 5)
            task.cancel()
            row = await db.get(CompanionDialogue, native_scope.id)
            assert row.status == "running"
            assert row.run_token is not None
            assert not task.done()
        await asyncio.wait_for(task, 5)
        row = (await native_rows(companion_db, CompanionDialogue))[0]
        assert row.status == "cancelled"
        assert row.run_token is None
        assert row.wake_at is None
        assert row.confirmed_deliveries == 1
        assert len(native_transport.network.wire) == 1
    finally:
        release.set()
        await manager.aclose()


@pytest.mark.asyncio
async def test_cancel_after_finish_commit_terminalizes_only_its_own_alarm(
    companion_db,
    native_transport,
    monkeypatch,
):
    native_scope = await enable(companion_db)
    committed, release = asyncio.Event(), asyncio.Event()
    original_finish = dialogue_store.finish

    async def committed_finish(*args, **kwargs):
        schedule = await original_finish(*args, **kwargs)
        committed.set()
        await release.wait()
        return schedule

    monkeypatch.setattr(dialogue_store, "finish", committed_finish)

    async def runner(batch):
        return DialogueOutcome(DialogueDecision("wait", 1), confirmed_deliveries=1)

    manager = DialogueManager(config(), runner, clock=Clock(), session_factory=companion_db.factory)
    try:
        await manager.admit(native_transport.session(), Contexts(), directed=True)
        await asyncio.wait_for(committed.wait(), 5)
        task = manager._owners[native_scope.id].task
        assert task is not None
        row = (await native_rows(companion_db, CompanionDialogue))[0]
        assert row.status == "scheduled"
        assert row.wake_at is not None
        await manager.cancel_scope(native_scope.id)
        release.set()
        await asyncio.wait_for(task, 5)
        row = (await native_rows(companion_db, CompanionDialogue))[0]
        assert row.status == "cancelled"
        assert row.run_token is None
        assert row.wake_at is None
        assert row.confirmed_deliveries == 1
    finally:
        release.set()
        await manager.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_owner", [False, True])
async def test_owner_creation_and_replacement_revalidate_mode_after_concurrent_sql_read(
    companion_db,
    native_transport,
    monkeypatch,
    existing_owner,
):
    native_scope = await enable(companion_db)
    clock, calls = Clock(), []
    reading, release, second_resolved = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_read, original_resolve = dialogue_store.read_schedule, dialogue_store.resolve_scope
    blocked = False

    async def read_schedule(mode, **kwargs):
        nonlocal blocked
        schedule = await original_read(mode, **kwargs)
        if not blocked:
            blocked = True
            reading.set()
            await release.wait()
        return schedule

    async def resolve_scope(identity, **kwargs):
        result = await original_resolve(identity, **kwargs)
        second_resolved.set()
        return result

    async def runner(batch):
        calls.append(batch)
        return DialogueOutcome(DialogueDecision("end"))

    manager = DialogueManager(config(), runner, clock=clock, session_factory=companion_db.factory)
    first = second = None
    try:
        await manager.start()
        if existing_owner:
            await manager.admit(native_transport.session(message="obsolete-old-owner"), Contexts(), directed=False)
            await store.set_mode(native_scope.id, "normal", persona_key="alpha", now=clock())
            await store.set_mode(native_scope.id, "companion", persona_key="alpha", now=clock())
        monkeypatch.setattr(dialogue_store, "read_schedule", read_schedule)
        first = asyncio.create_task(
            manager.admit(
                native_transport.session(message="first-revision"),
                Contexts(),
                directed=False,
            )
        )
        await asyncio.wait_for(reading.wait(), 5)
        await store.set_mode(native_scope.id, "normal", persona_key="alpha", now=clock())
        updated = await store.set_mode(native_scope.id, "companion", persona_key="alpha", now=clock())
        monkeypatch.setattr(dialogue_store, "resolve_scope", resolve_scope)
        second = asyncio.create_task(
            manager.admit(
                native_transport.session(message="new-revision"),
                Contexts(),
                directed=False,
            )
        )
        await asyncio.wait_for(second_resolved.wait(), 5)
        release.set()
        assert await asyncio.wait_for(asyncio.gather(first, second), 5) == [True, True]
        clock.advance(seconds=2)
        manager.wake()
        await until(lambda: len(calls) == 1 and not manager.active(native_scope.id))
        assert calls[0].mode == updated
        assert [item.session.event.message.id for item in calls[0].inputs] == ["first-revision", "new-revision"]
    finally:
        release.set()
        await manager.aclose()
        await asyncio.gather(*(task for task in (first, second) if task is not None), return_exceptions=True)


@pytest.mark.asyncio
async def test_deferred_initial_and_live_inputs_requeue_once_after_settlement(companion_db, native_transport):
    native_scope = await enable(companion_db)
    clock, batches, accepted = Clock(), [], []
    entered, defer_now, deferred, release = (asyncio.Event() for _ in range(4))
    root = native_transport.session(message="consumed-root")
    initial = native_transport.session(message="deferred-initial")
    live = native_transport.session(user="platform-bob", message="deferred-live")
    queued = native_transport.session(message="later-pending")
    contexts = [Contexts() for _ in range(4)]

    async def runner(batch):
        batches.append(batch)
        if len(batches) == 1:

            def admitter(item):
                accepted.append(item)
                return True

            batch.bind_admitter(admitter)
            entered.set()
            await defer_now.wait()
            assert not batch.defer_input(native_transport.session(message="deferred-initial"))
            assert batch.defer_input(live)
            assert batch.defer_input(initial)
            assert not batch.defer_input(live)
            assert not batch.defer_input(initial)
            deferred.set()
            await release.wait()
        return DialogueOutcome(DialogueDecision("end"))

    manager = DialogueManager(
        config(),
        runner,
        clock=clock,
        session_factory=companion_db.factory,
        max_pending_inputs=2,
        max_live_inputs=1,
    )
    try:
        await manager.admit(root, contexts[0], directed=False)
        await manager.admit(initial, contexts[1], directed=False)
        clock.advance(seconds=2)
        manager.wake()
        await asyncio.wait_for(entered.wait(), 5)
        await manager.admit(live, contexts[2], directed=True)
        await manager.admit(queued, contexts[3], directed=True)
        defer_now.set()
        await asyncio.wait_for(deferred.wait(), 5)
        assert [item.session for item in accepted] == [live]
        assert await batches[0].admit_live() == ()
        owner = manager._owners[native_scope.id]
        assert [item.session for item in owner.pending] == [queued]
        assert len(owner.deferred) == 2
        assert len(batches) == 1
        release.set()
        await until(lambda: len(batches) == 2 and not manager.active(native_scope.id))
        assert [item.session for item in batches[1].inputs] == [initial, live, queued]
        assert [item.ctx for item in batches[1].inputs] == contexts[1:]
        assert batches[1].inputs[0] is batches[0].inputs[1]
        assert batches[1].inputs[1] is accepted[0]
        assert not batches[0].defer_input(initial)
        assert not batches[1].defer_input(live)
        assert owner.admitted == {}
        assert owner.deferred == {}
    finally:
        defer_now.set()
        release.set()
        await manager.aclose()


@pytest.mark.asyncio
async def test_cancel_discards_deferred_unapplied_input_without_readmitting(companion_db, native_transport):
    native_scope = await enable(companion_db)
    entered, return_input, deferred = (asyncio.Event() for _ in range(3))
    calls, accepted = [], []
    live = native_transport.session(message="returned-before-cancel")

    async def runner(batch):
        calls.append(batch)
        batch.bind_admitter(lambda item: accepted.append(item) or True)
        entered.set()
        await return_input.wait()
        assert batch.defer_input(live)
        deferred.set()
        await asyncio.Event().wait()

    manager = DialogueManager(config(), runner, clock=Clock(), session_factory=companion_db.factory)
    try:
        await manager.admit(native_transport.session(message="root"), Contexts(), directed=True)
        await asyncio.wait_for(entered.wait(), 5)
        await manager.admit(live, Contexts(), directed=True)
        return_input.set()
        await asyncio.wait_for(deferred.wait(), 5)
        owner = manager._owners[native_scope.id]
        task = owner.task
        assert task is not None
        assert len(owner.deferred) == 1
        await manager.cancel_scope(native_scope.id)
        assert not calls[0].defer_input(live)
        await asyncio.wait_for(task, 5)
        assert len(calls) == 1
        assert [item.session for item in accepted] == [live]
        assert not owner.pending
        assert not owner.admitted
        assert not owner.deferred
        row = (await native_rows(companion_db, CompanionDialogue))[0]
        assert row.status == "cancelled"
        assert row.run_token is None
    finally:
        return_input.set()
        await manager.aclose()


@pytest.mark.asyncio
async def test_old_settlement_cas_cannot_cancel_replacement_running_token(companion_db):
    native_scope = await enable(companion_db)
    mode = await store.get_mode(native_scope.id)
    await dialogue_store.initialize_store(now=NOW, session_factory=companion_db.factory)
    first = await dialogue_store.begin(mode, now=NOW, session_factory=companion_db.factory)
    assert first is not None
    settled = await dialogue_store.finish(
        mode,
        first,
        DialogueDecision("wait", 1),
        status="completed",
        confirmed_deliveries=1,
        now=NOW,
        session_factory=companion_db.factory,
    )
    assert settled is not None
    replacement = await dialogue_store.begin(mode, now=NOW, session_factory=companion_db.factory)
    assert replacement is not None
    assert replacement != first
    await dialogue_store.cancel(
        native_scope.id,
        now=NOW,
        session_factory=companion_db.factory,
        expected=settled,
    )
    row = (await native_rows(companion_db, CompanionDialogue))[0]
    assert row.status == "running"
    assert row.run_token == replacement
    assert row.confirmed_deliveries == 1
    await dialogue_store.finish(
        mode,
        replacement,
        DialogueDecision("end"),
        status="cancelled",
        confirmed_deliveries=0,
        now=NOW,
        session_factory=companion_db.factory,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("idle_owner", [False, True])
async def test_idle_or_absent_owner_cancel_cannot_clear_new_running_claim(
    companion_db,
    native_transport,
    monkeypatch,
    idle_owner,
):
    native_scope = await enable(companion_db)
    cancel_entered, cancel_release = asyncio.Event(), asyncio.Event()
    run_entered, run_release = asyncio.Event(), asyncio.Event()
    batches, cancellations = [], []
    original_cancel = dialogue_store.cancel

    async def held_cancel(*args, **kwargs):
        cancellations.append(kwargs.get("expected"))
        cancel_entered.set()
        await cancel_release.wait()
        return await original_cancel(*args, **kwargs)

    async def runner(batch):
        batches.append(batch)
        if idle_owner and len(batches) == 1:
            return DialogueOutcome(DialogueDecision("wait", 60))
        run_entered.set()
        await run_release.wait()
        return DialogueOutcome(DialogueDecision("end"))

    manager = DialogueManager(config(), runner, clock=Clock(), session_factory=companion_db.factory)
    cancelling = None
    try:
        await manager.start()
        if idle_owner:
            await manager.admit(native_transport.session(message="old-idle-owner"), Contexts(), directed=True)
            await until(lambda: len(batches) == 1 and not manager.active(native_scope.id))
            previous = manager._owners[native_scope.id].schedule
            assert previous is not None
            assert previous.wake_at is not None
        else:
            assert native_scope.id not in manager._owners
            previous = None
        monkeypatch.setattr(dialogue_store, "cancel", held_cancel)
        cancelling = asyncio.create_task(manager.cancel_scope(native_scope.id))
        if idle_owner:
            await asyncio.wait_for(cancel_entered.wait(), 5)
            assert cancellations == [previous]
        else:
            # No owned row means no delayed write may target a future claim.
            await asyncio.wait_for(cancelling, 5)
            assert cancellations == []
        await manager.admit(native_transport.session(message="replacement-claim"), Contexts(), directed=True)
        await asyncio.wait_for(run_entered.wait(), 5)
        row = (await native_rows(companion_db, CompanionDialogue))[0]
        token = row.run_token
        assert row.status == "running"
        assert token is not None
        cancel_release.set()
        await asyncio.wait_for(cancelling, 5)
        row = (await native_rows(companion_db, CompanionDialogue))[0]
        assert row.status == "running"
        assert row.run_token == token
        assert await batches[-1].valid()
        run_release.set()
        await until(lambda: not manager.active(native_scope.id))
        row = (await native_rows(companion_db, CompanionDialogue))[0]
        assert row.status == "idle"
        assert row.run_token is None
        assert row.wake_at is None
    finally:
        cancel_release.set()
        run_release.set()
        await manager.aclose()
        if cancelling is not None:
            await asyncio.gather(cancelling, return_exceptions=True)
