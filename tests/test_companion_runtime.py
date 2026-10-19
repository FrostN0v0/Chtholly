"""Behavioral regressions for native companion claims, feedback and transport races."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from satori import Quote
from sqlalchemy import text
from arclet.entari import MessageChain
from companion_helpers import (
    NOW,
    Clock,
    scope,
    until,
    config,
    settled,
    response,
    native_rows,
    enable_member,
    install_provider,
)
from arclet.letoderea.context import Contexts

from plugins.llm_chat import agent_turn_setup
from utils.companion_core import LifePlan, LifeEvent, LifeState, AttentionUpdate, ContactProposal, ReflectionProposal
from plugins.llm_chat.models import AgentTurn, Conversation, RelationshipEvidence, ScopePersonaSelection
from plugins.llm_chat.identity import ChatIdentity
from plugins.llm_chat.companion import store
from plugins.llm_chat.image_inputs import ImageInputs
from plugins.llm_chat.channel_turns import steer_participant_turn
from plugins.llm_chat.core.delivery import DeliveryState
from plugins.llm_chat.group_delivery import group_delivery_scope
from plugins.llm_chat.tools._delivery import send_with_delivery
from plugins.llm_chat.companion.models import CompanionLife, CompanionMode, CompanionReview, CompanionChatEvidence
from plugins.llm_chat.companion.runtime import current_companion_service, install_companion_runtime
from plugins.llm_chat.companion.service import CompanionService

pytest_plugins = ["companion_helpers"]


def free_chat(member, *, key="quiet-conversation"):
    return ContactProposal(
        member_ref=member.member_ref,
        key=key,
        purpose="free_chat",
        topic="A quiet conversation about color",
        reason="A relaxed shared-interest conversation is welcome",
        evidence_turn_ids=[],
    )


async def prepare_chat(session, *, content="The exam is over and I feel relieved", user="alice-unified"):
    inputs = ImageInputs()
    prepared = await agent_turn_setup.prepare_agent_turn(
        config(),
        session,
        ChatIdentity(user_id=user, display_name=session.user.name or user, participant_ref=""),
        model_name="test-model",
        model_text=content,
        raw_user_text=content,
        image_inputs=inputs,
        content=content,
        current_content=None,
        forwarded_messages=(),
        mentioned_participants=(),
        warn=pytest.fail,
        tool_schemas=(),
    )
    return prepared


@pytest.mark.asyncio
async def test_missing_normal_and_off_modes_make_zero_requests_and_no_native_probes(
    companion_db, native_transport, monkeypatch
):
    native_scope = await scope(companion_db)
    payloads = install_provider(monkeypatch, [])
    worker = CompanionService(config(), accounts=lambda: [native_transport.account], clock=Clock())
    try:
        await worker.start()
        for _ in range(3):
            worker.wake()
            await settled(worker)
        assert await native_rows(companion_db, CompanionMode) == []
        assert await native_rows(companion_db, CompanionReview) == []
        assert await native_rows(companion_db, AgentTurn) == []
        assert payloads == native_transport.network.lookups == native_transport.network.wire == []
        await store.set_mode(native_scope.id, "normal", persona_key="alpha", now=NOW)
        await settled(worker)
        assert await native_rows(companion_db, CompanionMode) == []
        await store.set_mode(native_scope.id, "companion", persona_key="alpha", now=NOW + timedelta(hours=1))
        await store.set_mode(native_scope.id, "normal", persona_key="alpha", now=NOW)
        worker.wake()
        await settled(worker)
        assert payloads == native_transport.network.lookups == native_transport.network.wire == []
    finally:
        await worker.aclose()


@pytest.mark.asyncio
async def test_active_ordinary_chat_preserves_the_daily_reflection_slot(companion_db, native_transport, monkeypatch):
    native_scope = await scope(companion_db)
    await enable_member(companion_db, native_scope)
    payloads = install_provider(monkeypatch, [response(ReflectionProposal(life=LifeState()).model_dump_json())])
    entered, release = asyncio.Event(), asyncio.Event()
    clock = Clock()

    @steer_participant_turn
    async def ordinary_chat(session, ctx):
        entered.set()
        await release.wait()

    worker = CompanionService(
        config(companion_daily_reflections=1), accounts=lambda: [native_transport.account], clock=clock
    )
    ordinary = asyncio.create_task(ordinary_chat(native_transport.session(), Contexts()))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await worker.start()
        for _ in range(3):
            await settled(worker)
            clock.advance(minutes=1)
        assert payloads == []
        assert await native_rows(companion_db, CompanionReview) == []

        release.set()
        await asyncio.wait_for(ordinary, 5)
        await settled(worker)
        assert len(payloads) == 1
        reviews = await native_rows(companion_db, CompanionReview)
        assert [review.status for review in reviews] == ["completed"]
    finally:
        release.set()
        await asyncio.wait_for(ordinary, 5)
        await worker.aclose()


@pytest.mark.asyncio
async def test_completed_chats_do_not_pull_forward_an_hourly_model_review(companion_db, native_transport, monkeypatch):
    native_scope = await scope(companion_db)
    member = await enable_member(companion_db, native_scope)
    reflection = ReflectionProposal(life=LifeState(), review_after_minutes=60)
    payloads = install_provider(monkeypatch, [response(reflection.model_dump_json()) for _ in range(2)])
    clock = Clock()
    worker = CompanionService(config(), accounts=lambda: [native_transport.account], clock=clock)
    prepared_turns = []
    try:
        await worker.start()
        await settled(worker)
        assert len(payloads) == 1
        for minute in (1, 10, 30):
            clock.advance(minutes=minute - int((clock.now - NOW).total_seconds() / 60))
            incoming = native_transport.session(message=f"completed-chat-{minute}")
            prepared = await prepare_chat(incoming, content=f"A completed ordinary chat at minute {minute}")
            prepared_turns.append(prepared)
            await prepared.lifecycle.finalize_agent_turn("completed")
            await worker.record_chat(incoming, prepared)
            await settled(worker)
            clock.advance(minutes=1)
            await settled(worker)
            assert len(payloads) == 1
            assert (await store.get_mode(native_scope.id)).next_review_at == NOW + timedelta(hours=1)
        members = await store.read_members(native_scope.id, "alpha", now=clock.now)
        assert members[0].version == member.version + 3
        assert members[0].last_turn_id == prepared_turns[-1].agent_access.turn_id
        assert len(await native_rows(companion_db, CompanionChatEvidence)) == 4
        assert len(await native_rows(companion_db, CompanionReview)) == 1
        clock.advance(minutes=29)
        await settled(worker)
        assert len(payloads) == 2
        assert [row.status for row in await native_rows(companion_db, CompanionReview)] == ["completed", "completed"]
        assert native_transport.network.wire == []
    finally:
        await worker.aclose()
        for prepared in prepared_turns:
            await prepared.image_inputs.aclose()


@pytest.mark.asyncio
async def test_life_and_concerns_continue_across_no_contact_rounds_and_restart(companion_db, native_transport):
    native_scope = await scope(companion_db)
    member = await enable_member(companion_db, native_scope)
    clock = Clock()
    seen = []

    async def reflection(**kwargs):
        assert set(kwargs) == {"config", "scope", "claim", "snapshot", "limits", "now", "tool_schemas"}
        snapshot = kwargs["snapshot"]
        round_number = len(seen) + 1
        seen.append(snapshot)
        assert snapshot.life_version == round_number - 1
        assert snapshot.life.focus == ("" if round_number == 1 else f"Painting chapter {round_number - 1}")
        if round_number > 1:
            assert snapshot.life.plans[0].progress == f"Page {round_number - 1}"
            assert snapshot.attention[0]["summary"] == "Alice is awaiting a real exam result"
        return ReflectionProposal(
            life=LifeState(
                focus=f"Painting chapter {round_number}",
                interests=["imagined colors"],
                plans=[
                    LifePlan(
                        key="paint-book",
                        title="An imagined color book",
                        status="active",
                        progress=f"Page {round_number}",
                    )
                ],
                recent_events=[
                    LifeEvent(key=f"page-{round_number}", summary=f"Fictionally painted page {round_number}")
                ],
            ),
            attention=[
                AttentionUpdate(
                    member_ref=member.member_ref,
                    key="exam",
                    kind="concern",
                    summary="Alice is awaiting a real exam result",
                    status="open",
                    evidence_turn_ids=[member.last_turn_id],
                )
            ],
            review_after_minutes=10,
        )

    first = CompanionService(config(), accounts=lambda: [native_transport.account], reflection=reflection, clock=clock)
    try:
        await first.start()
        await settled(first)
        assert len(seen) == 1
        clock.advance(minutes=9)
        await settled(first)
        assert len(seen) == 1
        clock.advance(minutes=1)
        await settled(first)
        assert len(seen) == 2
    finally:
        await first.aclose()
    clock.advance(minutes=10)
    second = CompanionService(config(), accounts=lambda: [native_transport.account], reflection=reflection, clock=clock)
    try:
        await second.start()
        await settled(second)
        state = await store.read_companion_context(native_scope.id, "alpha", member.user_id)
        assert len(seen) == 3
        assert state["life"]["focus"] == "Painting chapter 3"
        assert state["life"]["plans"][0]["status"] == "active"
        assert state["attention"][0]["status"] == "open"
        assert await store.read_contact_history(native_scope.id, "alpha") == ()
        assert native_transport.network.wire == []
        assert (await store.get_mode(native_scope.id)).next_review_at == clock.now + timedelta(minutes=10)
    finally:
        await second.aclose()


@pytest.mark.asyncio
async def test_task_free_contact_uses_exact_native_target_and_real_receipt(companion_db, native_transport, monkeypatch):
    native_scope = await scope(companion_db)
    await store.set_mode(native_scope.id, "companion", persona_key="alpha", now=NOW - timedelta(minutes=10))
    member = await store.register_member(
        native_scope.id,
        "alpha",
        user_id="alice-unified",
        platform_user_id="platform-alice",
        display_name="Alice",
        now=NOW - timedelta(minutes=10),
    )
    assert member.last_turn_id == 0
    install_provider(monkeypatch, [])
    captured = []

    async def reflection(**kwargs):
        assert kwargs["snapshot"].attention == ()
        assert kwargs["snapshot"].member_turn_ids[member.member_ref] == ()
        return ReflectionProposal(life=LifeState(focus="Imagined painting afternoon"), contact=free_chat(member))

    async def runner(session, ctx, prepared, **kwargs):
        captured.append(prepared)
        assert session.account is native_transport.account
        assert session.channel.id == native_scope.channel_id
        assert session.user.id == member.platform_user_id
        assert session.event.message is None
        assert ctx["$event"] is session.event
        assert prepared.agent_access.user_id == member.user_id
        assert prepared.agent_access.trigger_kind == "autonomous"
        assert prepared.agent_access.raw_user_text == ""
        assert prepared.agent_access.is_operator is False
        await send_with_delivery(
            session,
            "I imagined a warm blue today. What colors do you enjoy?",
            prepared.delivery_state,
            texts=["I imagined a warm blue today. What colors do you enjoy?"],
        )
        await prepared.lifecycle.persist_delivered_text()
        return "completed"

    worker = CompanionService(
        config(), accounts=lambda: [native_transport.account], reflection=reflection, runner=runner, clock=Clock()
    )
    try:
        await worker.start()
        await settled(worker)
        history = await store.read_contact_history(native_scope.id, "alpha")
        assert len(history) == len(captured) == len(native_transport.network.wire) == 1
        assert history[0]["status"] == "delivered"
        assert history[0]["receipts"] == ("native-receipt-1",)
        assert history[0]["turn_id"] == captured[0].agent_access.turn_id
        assert history[0]["proposal"]["evidence_turn_ids"] == ()
        assert native_transport.network.wire[0]["channel_id"] == native_scope.channel_id
        assert list(MessageChain(native_transport.network.wire[0]["content"]).select(Quote)) == []
        assert (await store.read_companion_context(native_scope.id, "alpha", member.user_id))["life"][
            "focus"
        ] == "Imagined painting afternoon"
        conversations = await native_rows(companion_db, Conversation)
        assert [row.role for row in conversations] == ["assistant"]
        assert await native_rows(companion_db, CompanionChatEvidence) == []
        assert await native_rows(companion_db, RelationshipEvidence) == []
        native = (await native_rows(companion_db, AgentTurn))[0]
        assert native.trigger_kind == "autonomous"
        assert native.conversation_user_id is None
        assert native.trigger_message_id == ""
        assert native.status == "completed"
    finally:
        await worker.aclose()


@pytest.mark.asyncio
async def test_off_cancels_blocked_reflection_and_rejects_late_plan(companion_db, native_transport):
    native_scope = await scope(companion_db)
    member = await enable_member(companion_db, native_scope)
    entered, cancelled = asyncio.Event(), asyncio.Event()
    calls = []

    async def reflection(**kwargs):
        calls.append(kwargs["claim"])
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            return ReflectionProposal(life=LifeState(focus="Stale plan must never land"), contact=free_chat(member))

    worker = CompanionService(
        config(), accounts=lambda: [native_transport.account], reflection=reflection, clock=Clock()
    )
    try:
        await worker.start()
        await asyncio.wait_for(entered.wait(), 5)
        await store.set_mode(native_scope.id, "normal", persona_key="alpha", now=NOW)
        await worker.cancel_scope(native_scope.id)
        await asyncio.wait_for(worker.drain(), 5)
        assert cancelled.is_set()
        assert (await store.get_mode(native_scope.id)).mode == "normal"
        assert await native_rows(companion_db, CompanionLife) == []
        assert await store.read_contact_history(native_scope.id, "alpha") == ()
        worker.wake()
        await settled(worker)
        assert len(calls) == 1
        assert native_transport.network.wire == []
    finally:
        await worker.aclose()


@pytest.mark.asyncio
async def test_off_rejects_queued_send_before_transport_without_attempt(companion_db, native_transport, monkeypatch):
    native_scope = await scope(companion_db)
    member = await enable_member(companion_db, native_scope)
    install_provider(monkeypatch, [])
    native_transport.network.hold = "ordinary-blocker"
    ordinary = native_transport.session(user="platform-bob")

    async def block_channel():
        with group_delivery_scope(ordinary):
            await send_with_delivery(ordinary, "ordinary-blocker", DeliveryState(), texts=["ordinary-blocker"])

    blocker = asyncio.create_task(block_channel())
    await asyncio.wait_for(native_transport.network.entered.wait(), 5)
    queued = asyncio.Event()
    captured = []

    async def reflection(**kwargs):
        return ReflectionProposal(life=LifeState(), contact=free_chat(member))

    async def runner(session, ctx, prepared, **kwargs):
        captured.append(prepared)
        queued.set()
        await send_with_delivery(session, "obsolete-autonomous", prepared.delivery_state, texts=["obsolete-autonomous"])
        return "completed"

    worker = CompanionService(
        config(), accounts=lambda: [native_transport.account], reflection=reflection, runner=runner, clock=Clock()
    )
    try:
        await worker.start()
        await asyncio.wait_for(queued.wait(), 5)
        await store.set_mode(native_scope.id, "normal", persona_key="alpha", now=NOW)
        await worker.cancel_scope(native_scope.id)
        await asyncio.wait_for(worker.drain(), 5)
        native_transport.network.release.set()
        await asyncio.wait_for(blocker, 5)
        assert len(native_transport.network.wire) == 1
        assert captured[0].delivery_state.delivery_attempts == captured[0].delivery_state.confirmed_deliveries == 0
        history = await store.read_contact_history(native_scope.id, "alpha")
        assert history[0]["status"] == "cancelled"
        assert history[0]["transport_started_at"] is None
        assert history[0]["receipts"] == ()
        async with companion_db.factory() as db:
            assert (await db.get(AgentTurn, history[0]["turn_id"])).status == "cancelled"
    finally:
        native_transport.network.release.set()
        await worker.aclose()
        await asyncio.gather(blocker, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_after_native_ack_before_receipt_commit_preserves_confirmed_prefix(
    companion_db, native_transport, monkeypatch
):
    native_scope = await scope(companion_db)
    member = await enable_member(companion_db, native_scope)
    install_provider(monkeypatch, [])
    native_transport.network.hold = "confirmed-prefix"
    captured = []
    finished = asyncio.Event()

    async def reflection(**kwargs):
        return ReflectionProposal(life=LifeState(), contact=free_chat(member))

    async def runner(session, ctx, prepared, **kwargs):
        captured.append(prepared)
        try:
            await send_with_delivery(session, "confirmed-prefix", prepared.delivery_state, texts=["confirmed-prefix"])
            await send_with_delivery(session, "must-not-follow", prepared.delivery_state, texts=["must-not-follow"])
            return "completed"
        finally:
            finished.set()

    worker = CompanionService(
        config(), accounts=lambda: [native_transport.account], reflection=reflection, runner=runner, clock=Clock()
    )
    connection = None
    off = None
    try:
        await worker.start()
        await asyncio.wait_for(native_transport.network.entered.wait(), 5)
        # Delay the real SQLite receipt commit without replacing any store function.
        connection = await companion_db.engine.connect()
        await connection.execute(text("BEGIN IMMEDIATE"))
        native_transport.network.release.set()
        await until(lambda: captured[0].delivery_state.confirmed_deliveries == 1)
        off = asyncio.create_task(store.set_mode(native_scope.id, "normal", persona_key="alpha", now=NOW))
        await worker.cancel_scope(native_scope.id)
        await connection.commit()
        await connection.close()
        connection = None
        await asyncio.wait_for(off, 5)
        await asyncio.wait_for(worker.drain(), 5)
        assert finished.is_set()
        assert len(native_transport.network.wire) == 1
        delivery = captured[0].delivery_state
        assert delivery.delivery_attempts == delivery.confirmed_deliveries == 1
        assert delivery.delivered_texts == ["confirmed-prefix"]
        history = await store.read_contact_history(native_scope.id, "alpha")
        assert history[0]["receipts"] == ("native-receipt-1",)
        assert history[0]["status"] == "delivered"
        conversations = await native_rows(companion_db, Conversation)
        assert [(row.role, row.content) for row in conversations] == [("assistant", "confirmed-prefix")]
        async with companion_db.factory() as db:
            assert (await db.get(AgentTurn, history[0]["turn_id"])).status == "partial"
    finally:
        native_transport.network.release.set()
        if connection is not None:
            await connection.rollback()
            await connection.close()
        if off is not None:
            await asyncio.gather(off, return_exceptions=True)
        await worker.aclose()


@pytest.mark.asyncio
async def test_real_chat_interrupts_old_idea_and_resolves_only_own_concern(companion_db, native_transport, monkeypatch):
    native_scope = await scope(companion_db)
    chatted_at = NOW - timedelta(minutes=15)
    alice = await enable_member(companion_db, native_scope, at=chatted_at)
    bob = await enable_member(
        companion_db, native_scope, user="bob-unified", platform_user="platform-bob", at=chatted_at
    )
    seeded_at = NOW - timedelta(minutes=10)
    initial = await store.claim_review(native_scope.id, "alpha", now=seeded_at)
    assert await store.finish_review(
        initial,
        ReflectionProposal(
            life=LifeState(focus="Ongoing painting"),
            attention=[
                AttentionUpdate(
                    member_ref=member.member_ref,
                    key="exam",
                    kind="concern",
                    summary=f"{member.display_name} awaits results",
                    status="open",
                    evidence_turn_ids=[member.last_turn_id],
                )
                for member in (alice, bob)
            ],
        ),
        now=seeded_at,
        next_review_at=NOW,
    )
    install_provider(monkeypatch, [])
    entered, stopped = asyncio.Event(), asyncio.Event()
    calls = []
    clock = Clock()

    async def reflection(**kwargs):
        calls.append(kwargs["snapshot"])
        if len(calls) == 1:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                stopped.set()
                return ReflectionProposal(life=LifeState(focus="Obsolete"), contact=free_chat(alice))
        snapshot = kwargs["snapshot"]
        current_alice = next(member for member in snapshot.members if member.user_id == alice.user_id)
        assert current_alice.last_turn_id != alice.last_turn_id
        return ReflectionProposal(
            life=snapshot.life,
            attention=[
                AttentionUpdate(
                    member_ref=alice.member_ref,
                    key="exam",
                    kind="concern",
                    summary="Alice reports the exam is over",
                    status="resolved",
                    evidence_turn_ids=[current_alice.last_turn_id],
                )
            ],
        )

    worker = CompanionService(config(), accounts=lambda: [native_transport.account], reflection=reflection, clock=clock)
    prepared = None
    try:
        await worker.start()
        await asyncio.wait_for(entered.wait(), 5)
        incoming = native_transport.session()
        worker.interrupt_input(incoming)
        await asyncio.wait_for(worker.drain(), 5)
        assert stopped.is_set()
        prepared = await prepare_chat(incoming)
        await prepared.lifecycle.finalize_agent_turn("completed")
        await worker.record_chat(incoming, prepared)
        members = await store.read_members(native_scope.id, "alpha", now=clock.now)
        updated = next(member for member in members if member.user_id == alice.user_id)
        assert updated.version == alice.version + 1
        assert updated.last_turn_id == prepared.agent_access.turn_id
        assert len(calls) == 1
        assert (await store.read_companion_context(native_scope.id, "alpha", alice.user_id))["attention"][0][
            "status"
        ] == "open"
        clock.advance(minutes=4)
        await settled(worker)
        assert len(calls) == 1
        clock.advance(minutes=1)
        await settled(worker)
        assert len(calls) == 2
        assert (await store.read_companion_context(native_scope.id, "alpha", alice.user_id))["attention"][0][
            "status"
        ] == "resolved"
        assert (await store.read_companion_context(native_scope.id, "alpha", bob.user_id))["attention"][0][
            "status"
        ] == "open"
        assert (await store.read_companion_context(native_scope.id, "alpha", alice.user_id))["life"][
            "focus"
        ] == "Ongoing painting"
        assert native_transport.network.wire == []
    finally:
        await worker.aclose()
        if prepared is not None:
            await prepared.image_inputs.aclose()


@pytest.mark.asyncio
async def test_unknown_native_result_never_replays_same_contact_even_after_restart(
    companion_db, native_transport, monkeypatch
):
    native_scope = await scope(companion_db)
    member = await enable_member(companion_db, native_scope)
    install_provider(monkeypatch, [])
    native_transport.network.outcome = "unknown"
    calls = []
    clock = Clock()

    async def reflection(**kwargs):
        calls.append(kwargs["snapshot"])
        return ReflectionProposal(life=LifeState(focus="Continuing fictional life"), contact=free_chat(member))

    async def runner(session, ctx, prepared, **kwargs):
        await send_with_delivery(session, "unknown-contact", prepared.delivery_state, texts=["unknown-contact"])
        return "completed"

    first = CompanionService(
        config(), accounts=lambda: [native_transport.account], reflection=reflection, runner=runner, clock=clock
    )
    try:
        await first.start()
        await settled(first)
        history = await store.read_contact_history(native_scope.id, "alpha")
        assert history[0]["status"] == "unknown"
        assert history[0]["receipts"] == ()
        assert len(native_transport.network.wire) == 1
        await settled(first)
        assert len(calls) == 1
    finally:
        await first.aclose()
    clock.advance(days=1)
    native_transport.network.outcome = "success"
    second = CompanionService(
        config(), accounts=lambda: [native_transport.account], reflection=reflection, runner=runner, clock=clock
    )
    try:
        await second.start()
        await settled(second)
        assert len(calls) == 2
        assert len(native_transport.network.wire) == 1
        assert len(await store.read_contact_history(native_scope.id, "alpha")) == 1
        assert (await store.read_companion_context(native_scope.id, "alpha", member.user_id))["life"][
            "focus"
        ] == "Continuing fictional life"
    finally:
        await second.aclose()


@pytest.mark.asyncio
async def test_persona_switch_cancels_old_review_and_scopes_do_not_share_life(companion_db, native_transport):
    first_scope = await scope(companion_db)
    other_scope = await scope(companion_db, channel="other-companion-group")
    await store.set_mode(first_scope.id, "companion", persona_key="alpha", now=NOW)
    await store.set_mode(other_scope.id, "companion", persona_key="alpha", now=NOW)
    entered, cancelled = asyncio.Event(), asyncio.Event()
    snapshots = []

    async def reflection(**kwargs):
        snapshots.append((kwargs["scope"].id, kwargs["claim"].persona_key, kwargs["snapshot"].life.focus))
        if kwargs["scope"].id == first_scope.id and kwargs["claim"].persona_key == "alpha":
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                return ReflectionProposal(life=LifeState(focus="Old Alpha state"))
        focus = "Other scope private life" if kwargs["scope"].id == other_scope.id else "Beta independent life"
        return ReflectionProposal(life=LifeState(focus=focus))

    worker = CompanionService(
        config(), accounts=lambda: [native_transport.account], reflection=reflection, clock=Clock()
    )
    try:
        await worker.start()
        await asyncio.wait_for(entered.wait(), 5)
        async with companion_db.factory() as db, db.begin():
            (await db.get(ScopePersonaSelection, first_scope.id)).persona_key = "beta"
        await worker.claim_ready()
        await asyncio.wait_for(worker.drain(), 5)
        await settled(worker)
        assert cancelled.is_set()
        assert (await store.get_mode(first_scope.id)).persona_key == "beta"
        assert await store.read_companion_context(first_scope.id, "alpha", "alice-unified") is None
        assert (await store.read_companion_context(first_scope.id, "beta", "alice-unified"))["life"][
            "focus"
        ] == "Beta independent life"
        assert (await store.read_companion_context(other_scope.id, "alpha", "alice-unified"))["life"][
            "focus"
        ] == "Other scope private life"
        assert (first_scope.id, "beta", "") in snapshots
        async with companion_db.factory() as db:
            assert await db.get(CompanionLife, (first_scope.id, "alpha")) is None
    finally:
        await worker.aclose()


@pytest.mark.asyncio
async def test_clean_unload_drains_worker_and_old_disposal_keeps_replacement_runtime(companion_db, native_transport):
    native_scope = await scope(companion_db)
    await enable_member(companion_db, native_scope)
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def reflection(**kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    first = CompanionService(
        config(), accounts=lambda: [native_transport.account], reflection=reflection, clock=Clock()
    )
    second = CompanionService(config(), accounts=lambda: [], clock=Clock())
    dispose_first = install_companion_runtime(first)
    dispose_second = install_companion_runtime(second)
    try:
        await first.start()
        await asyncio.wait_for(entered.wait(), 5)
        await asyncio.wait_for(first.aclose(), 5)
        assert cancelled.is_set()
        dispose_first()
        assert current_companion_service() is second
        before = len(await native_rows(companion_db, CompanionReview))
        first.wake()
        first.account_update(native_transport.account)
        await first.claim_ready()
        await first.drain()
        assert len(await native_rows(companion_db, CompanionReview)) == before
        assert all(row.status != "active" for row in await native_rows(companion_db, CompanionReview))
        assert native_transport.network.wire == []
    finally:
        dispose_second()
        dispose_first()
        await first.aclose()
        await second.aclose()


@pytest.mark.asyncio
async def test_completed_chat_captured_before_off_on_does_not_enter_new_mode_epoch(
    companion_db, native_transport, monkeypatch
):
    native_scope = await scope(companion_db)
    member = await enable_member(companion_db, native_scope)
    payloads = install_provider(monkeypatch, [])
    session = native_transport.session()
    prepared = await prepare_chat(session)
    worker = CompanionService(config(), accounts=lambda: [native_transport.account], clock=Clock())
    try:
        captured_revision = prepared.companion_revision
        await store.set_mode(native_scope.id, "normal", persona_key="alpha", now=NOW)
        current = await store.set_mode(native_scope.id, "companion", persona_key="alpha", now=NOW)
        assert current.revision > captured_revision
        await prepared.lifecycle.finalize_agent_turn("completed")
        await worker.record_chat(session, prepared)
        members = await store.read_members(native_scope.id, "alpha", now=NOW)
        assert members[0].version == member.version
        assert members[0].last_turn_id == member.last_turn_id
        assert len(await native_rows(companion_db, CompanionChatEvidence)) == 1
        assert payloads == []
    finally:
        await worker.aclose()
        await prepared.image_inputs.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "cancelled", "partial"])
async def test_unqualified_native_chat_completion_does_not_register_feedback(
    companion_db, native_transport, monkeypatch, status
):
    native_scope = await scope(companion_db)
    member = await enable_member(companion_db, native_scope)
    payloads = install_provider(monkeypatch, [])
    session = native_transport.session()
    prepared = await prepare_chat(session)
    worker = CompanionService(config(), accounts=lambda: [native_transport.account], clock=Clock())
    try:
        await prepared.lifecycle.finalize_agent_turn(status)
        await worker.record_chat(session, prepared)
        members = await store.read_members(native_scope.id, "alpha", now=NOW)
        assert members[0].version == member.version
        assert members[0].last_turn_id == member.last_turn_id
        assert len(await native_rows(companion_db, CompanionChatEvidence)) == 1
        assert payloads == []
    finally:
        await worker.aclose()
        await prepared.image_inputs.aclose()


@pytest.mark.asyncio
async def test_native_account_connection_wake_reserves_only_one_review(companion_db, native_transport, monkeypatch):
    from companion_helpers import response

    native_scope = await scope(companion_db)
    await enable_member(companion_db, native_scope)
    native_transport.account.connected.clear()
    payloads = install_provider(
        monkeypatch, [response(ReflectionProposal(life=LifeState(focus="A quiet online afternoon")).model_dump_json())]
    )
    worker = CompanionService(config(), accounts=lambda: [native_transport.account], clock=Clock())
    try:
        await worker.start()
        await settled(worker)
        assert payloads == []
        assert await native_rows(companion_db, CompanionReview) == []
        native_transport.account.connected.set()
        worker.account_update(native_transport.account)
        await asyncio.gather(worker.claim_ready(), worker.claim_ready())
        await settled(worker)
        assert len(payloads) == len(await native_rows(companion_db, CompanionReview)) == 1
        assert (await store.read_companion_context(native_scope.id, "alpha", "alice-unified"))["life"][
            "focus"
        ] == "A quiet online afternoon"
        assert native_transport.network.wire == []
    finally:
        await worker.aclose()


@pytest.mark.asyncio
async def test_real_preference_revocation_cancels_native_contact_before_send(
    companion_db, native_transport, monkeypatch
):
    from plugins.llm_chat.models import ReminderPreference

    native_scope = await scope(companion_db)
    member = await enable_member(companion_db, native_scope)
    install_provider(monkeypatch, [])
    entered, cancelled = asyncio.Event(), asyncio.Event()
    captured = []

    async def reflection(**kwargs):
        return ReflectionProposal(life=LifeState(), contact=free_chat(member))

    async def runner(session, ctx, prepared, **kwargs):
        captured.append(prepared)
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    worker = CompanionService(
        config(), accounts=lambda: [native_transport.account], reflection=reflection, runner=runner, clock=Clock()
    )
    try:
        await worker.start()
        await asyncio.wait_for(entered.wait(), 5)
        async with companion_db.factory() as db, db.begin():
            db.add(
                ReminderPreference(
                    account_id=native_transport.account.self_id, unified_user_id=member.user_id, enabled=False
                )
            )
        await worker.claim_ready()
        await asyncio.wait_for(worker.drain(), 5)
        assert cancelled.is_set()
        assert captured[0].delivery_state.delivery_attempts == 0
        history = await store.read_contact_history(native_scope.id, "alpha")
        assert history[0]["status"] == "cancelled"
        assert history[0]["transport_started_at"] is None
        assert native_transport.network.wire == []
    finally:
        await worker.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("purpose", "trigger"),
    [
        (purpose, trigger)
        for purpose, expected in (
            ("chat", "message"),
            ("reminder", "scheduled"),
            ("companion", "autonomous"),
            ("companion_reflection", "reflection"),
        )
        for trigger in ("message", "scheduled", "autonomous", "reflection")
        if trigger != expected
    ],
)
async def test_native_turn_cannot_cross_session_purpose(companion_db, purpose, trigger):
    from plugins.llm_chat import context_builder, session_manager

    native_scope = await scope(companion_db)
    baseline = context_builder.build_baseline_fingerprint(model_name="test-model", persona="Alpha", tool_schemas=())
    context = await session_manager.create_session(
        native_scope.id,
        baseline,
        start_reason="purpose-boundary",
        purpose=purpose,
        reminder_task_ref="existing-task" if purpose == "reminder" else "",
    )
    try:
        await session_manager.start_turn(
            context,
            trigger_message_id="",
            user_id="alice-unified",
            user_name="Alice",
            conversation_user_id=None,
            fresh_context=True,
            trigger_kind=trigger,
        )
    except ValueError:
        pass
    else:
        pytest.fail("A turn from a different session purpose was accepted")
    assert await native_rows(companion_db, AgentTurn) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("defer_before_guard", [False, True], ids=["during-guard", "before-guard"])
async def test_late_child_transport_loses_closed_generation_authority(
    companion_db,
    native_transport,
    defer_before_guard,
):
    from plugins.llm_chat import context_builder, session_manager
    from plugins.llm_chat.companion.context import companion_limits
    from plugins.llm_chat.companion.delivery import (
        CompanionLeaseLost,
        send_companion_transport,
        companion_transport_scope,
    )

    native_scope = await scope(companion_db)
    member = await enable_member(companion_db, native_scope)
    proposal = free_chat(member)
    review = await store.claim_review(native_scope.id, "alpha", now=NOW)
    assert review is not None
    assert await store.finish_review(
        review,
        ReflectionProposal(life=LifeState(), contact=proposal),
        now=NOW,
        next_review_at=NOW + timedelta(hours=1),
    )
    claim = await store.claim_contact(
        native_scope.id,
        "alpha",
        member.member_ref,
        proposal,
        origin=review,
        now=NOW,
        limits=companion_limits(config()),
    )
    assert claim is not None
    baseline = context_builder.build_baseline_fingerprint(model_name="test-model", persona="Alpha", tool_schemas=())
    context = await session_manager.create_session(
        native_scope.id,
        baseline,
        start_reason="transport-ownership",
        purpose="companion",
    )
    turn = await session_manager.start_turn(
        context,
        trigger_message_id="",
        user_id=member.user_id,
        user_name="Alice",
        conversation_user_id=None,
        fresh_context=True,
        trigger_kind="autonomous",
    )
    assert await store.bind_contact_turn(claim, turn.id, now=NOW)
    entered, release = asyncio.Event(), asyncio.Event()

    async def guard():
        if not defer_before_guard:
            entered.set()
            await release.wait()
        return await store.contact_valid(claim, now=NOW)

    async def child_send():
        if defer_before_guard:
            entered.set()
            await release.wait()
        return await send_companion_transport(lambda: native_transport.session().send("obsolete-child-send"))

    pending = None
    try:
        with companion_transport_scope(claim, guard, clock=Clock()):
            pending = asyncio.create_task(child_send())
            await asyncio.wait_for(entered.wait(), 5)
        release.set()
        with pytest.raises(CompanionLeaseLost):
            await asyncio.wait_for(pending, 5)
        history = await store.read_contact_history(native_scope.id, "alpha")
        assert history[0]["transport_started_at"] is None
        assert history[0]["receipts"] == ()
        assert native_transport.network.wire == []
    finally:
        release.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        await store.finish_contact(claim, status="cancelled", now=NOW)
        await session_manager.finish_turn(turn.id, status="cancelled")
