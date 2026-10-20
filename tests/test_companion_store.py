"""Deterministic actual-SQLite invariants for autonomous companion persistence."""

from __future__ import annotations

from types import SimpleNamespace
import asyncio
from pathlib import Path
from datetime import datetime, timezone, timedelta
from dataclasses import replace

import pytest
from sqlalchemy import func, select
from arclet.entari.config import EntariConfig
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

if not hasattr(EntariConfig, "instance"):
    EntariConfig.instance = EntariConfig.load(Path(__file__).resolve().parents[1] / "entari.full.example.yml")

from entari_plugin_database import Base

from utils.companion_core import (
    LifePlan,
    LifeEvent,
    LifeState,
    AttentionUpdate,
    CompanionLimits,
    ContactProposal,
    ReflectionProposal,
)
from plugins.llm_chat.models import (
    AgentTurn,
    ChatScope,
    ReminderTask,
    UserRelation,
    ContextSession,
    ReminderPreference,
    ScopePersonaSelection,
)
from plugins.llm_chat.companion import store
from plugins.llm_chat.companion.types import ReviewClaim, CompanionConflict, CompanionCorruptState
from plugins.llm_chat.companion.models import (
    CompanionLife,
    CompanionMode,
    CompanionReview,
    CompanionChatEvidence,
)

UTC = timezone.utc
NOW = datetime(2026, 10, 10, 2, 0, tzinfo=UTC)
LIMITS = CompanionLimits()


@pytest.fixture
async def companion_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'companion.db'}", connect_args={"timeout": 15})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        tables = [table for table in Base.metadata.sorted_tables if not table.name.startswith("chat_companion_")]
        await connection.run_sync(lambda conn: Base.metadata.create_all(conn, tables=tables))
    monkeypatch.setattr(store, "get_session", factory)
    async with factory() as db, db.begin():
        db.add_all(
            [
                ChatScope(id=1, platform="qq", account_id="bot", channel_id="group1"),
                ChatScope(id=2, platform="qq", account_id="bot", channel_id="group2"),
                ScopePersonaSelection(scope_id=1, persona_key="alice"),
                ScopePersonaSelection(scope_id=2, persona_key="alice"),
                ReminderTask(
                    task_ref="existing-reminder",
                    account_id="bot",
                    unified_user_id="explicit-user",
                    goal="Existing explicit reminder",
                    due_at=NOW.replace(tzinfo=None),
                ),
                ReminderPreference(account_id="bot", unified_user_id="explicit-user", enabled=False),
                UserRelation(user_id="u1", channel_id="group1", affection=42.0, trust=53.0),
            ]
        )
    await store.initialize_store(now=NOW)
    try:
        yield SimpleNamespace(engine=engine, factory=factory)
    finally:
        await engine.dispose()


async def turn(database, *, scope=1, user="u1", at=NOW, purpose="chat", trigger="message", status="completed"):
    async with database.factory() as db, db.begin():
        sequence = (
            await db.scalar(select(func.max(ContextSession.sequence)).where(ContextSession.scope_id == scope)) or 0
        ) + 1
        session = ContextSession(scope_id=scope, sequence=sequence, purpose=purpose)
        db.add(session)
        await db.flush()
        row = AgentTurn(
            session_id=session.id,
            sequence=1,
            user_id=user,
            user_name=user,
            trigger_kind=trigger,
            status=status,
            created_at=(at - timedelta(seconds=1)).replace(tzinfo=None),
            finished_at=at.replace(tzinfo=None) if status != "running" else None,
        )
        db.add(row)
        await db.flush()
        return row.id


async def enable_member(database, *, scope=1, user="u1", platform_user="p1", at=NOW - timedelta(minutes=10), real=True):
    await store.set_mode(scope, "companion", persona_key="alice", now=at)
    if not real:
        return await store.register_member(
            scope,
            "alice",
            user_id=user,
            platform_user_id=platform_user,
            display_name=user,
            now=at,
        )
    turn_id = await turn(database, scope=scope, user=user, at=at)
    return await store.record_chat(
        scope,
        "alice",
        user_id=user,
        platform_user_id=platform_user,
        display_name=user,
        turn_id=turn_id,
        now=at,
    )


def proposal(member, key="chat-1", *, purpose="free_chat", evidence=()):
    return ContactProposal(
        member_ref=member.member_ref,
        key=key,
        purpose=purpose,
        topic="A quiet conversation",
        reason="A natural opportunity to talk",
        evidence_turn_ids=list(evidence),
    )


async def review_for_contact(database, member, p, *, at=NOW):
    async with database.factory() as db, db.begin():
        mode = await db.get(CompanionMode, member.scope_id)
        if mode is None or mode.mode != "companion" or mode.persona_key != member.persona_key:
            return None
        active = await db.get(CompanionReview, mode.review_token) if mode.review_token else None
        if active is not None and active.status == "active" and active.lease_deadline > at.replace(tzinfo=None):
            origin = ReviewClaim(
                active.scope_id,
                active.persona_key,
                active.mode_revision,
                active.context_revision,
                active.lease_token,
                active.lease_deadline.replace(tzinfo=UTC),
                active.life_version,
            )
        else:
            origin = None
            mode.next_review_at = at.replace(tzinfo=None)
    if origin is None:
        origin = await store.claim_review(member.scope_id, member.persona_key, now=at, daily_limit=48)
    assert origin is not None
    snapshot = await store.load_reflection(origin, now=at)
    assert await store.finish_review(
        origin,
        ReflectionProposal(life=snapshot.life, contact=p),
        now=at,
        next_review_at=at + timedelta(hours=1),
    )
    return origin


async def contact(database, member, *, at=NOW, key="chat-1", limits=LIMITS, lease_seconds=300):
    p = proposal(member, key)
    origin = await review_for_contact(database, member, p, at=at)
    if origin is None:
        return None
    claim = await store.claim_contact(
        member.scope_id,
        member.persona_key,
        member.member_ref,
        p,
        origin=origin,
        now=at,
        limits=limits,
        lease_seconds=lease_seconds,
    )
    if claim is not None:
        native_id = await turn(
            database,
            scope=member.scope_id,
            user=member.user_id,
            at=at,
            purpose="companion",
            trigger="autonomous",
            status="running",
        )
        assert await store.bind_contact_turn(claim, native_id, now=at)
    return claim


@pytest.mark.asyncio
async def test_normal_reads_do_not_create_state_or_seed_evidence(companion_db):
    mode = await store.get_mode(1, session_factory=companion_db.factory)
    assert mode.mode == "normal"
    assert mode.revision == 0
    assert await store.read_companion_context(1, "alice", "u1", session_factory=companion_db.factory) is None
    assert (
        await store.register_member(1, "alice", user_id="u1", platform_user_id="p1", display_name="One", now=NOW)
        is None
    )
    assert (
        await store.record_chat(
            1, "alice", user_id="u1", platform_user_id="p1", display_name="One", turn_id=999, now=NOW
        )
        is None
    )
    assert await store.due_scopes(now=NOW) == ()
    async with companion_db.factory() as db:
        assert await db.scalar(select(func.count()).select_from(CompanionMode)) == 0
        assert await db.scalar(select(func.count()).select_from(CompanionLife)) == 0
        assert (
            await db.get(ReminderTask, (await db.scalar(select(ReminderTask.id))))
        ).goal == "Existing explicit reminder"
        assert (await db.get(ReminderPreference, ("bot", "explicit-user"))).enabled is False


@pytest.mark.asyncio
async def test_mode_noop_and_off_invalidate_without_erasing_state(companion_db):
    await enable_member(companion_db)
    first = await store.get_mode(1)
    review = await store.claim_review(1, "alice", now=NOW)
    assert review is not None
    same = await store.set_mode(1, "companion", persona_key="alice", now=NOW + timedelta(seconds=1))
    assert same == first
    assert await store.finish_review(
        review,
        ReflectionProposal(life=LifeState(focus="Painting")),
        now=NOW,
        next_review_at=NOW + timedelta(hours=1),
    )
    claim = await contact(companion_db, (await store.read_members(1, "alice", now=NOW))[0])
    assert claim is not None
    off = await store.set_mode(
        1, "normal", persona_key="alice", now=NOW + timedelta(seconds=2), expected_revision=first.revision
    )
    assert off.revision == first.revision + 1
    assert off.next_review_at is None
    assert not await store.record_transport_started(claim, now=NOW + timedelta(seconds=3))
    assert (await store.read_contact_history(1, "alice"))[0]["status"] == "cancelled"
    assert await store.read_companion_context(1, "alice", "u1") is None
    restored = await store.set_mode(1, "companion", persona_key="alice", now=NOW + timedelta(minutes=20))
    assert restored.next_review_at == NOW + timedelta(minutes=20)
    assert (await store.read_companion_context(1, "alice", "u1"))["life"]["focus"] == "Painting"
    assert not await store.finish_review(
        review, ReflectionProposal(life=LifeState(focus="Old")), now=NOW, next_review_at=NOW
    )


@pytest.mark.asyncio
async def test_scope_and_persona_states_are_private_and_rebinding_fences(companion_db):
    await enable_member(companion_db)
    review = await store.claim_review(1, "alice", now=NOW)
    async with companion_db.factory() as db, db.begin():
        (await db.get(ScopePersonaSelection, 1)).persona_key = "bob"
    rebound = await store.rebind_persona(1, "bob", now=NOW)
    assert rebound.revision > review.mode_revision
    assert not await store.finish_review(
        review, ReflectionProposal(life=LifeState(focus="Alice secret")), now=NOW, next_review_at=NOW
    )
    bob = await store.claim_review(1, "bob", now=NOW)
    assert bob is not None
    assert await store.finish_review(
        bob, ReflectionProposal(life=LifeState(focus="Bob hobby")), now=NOW, next_review_at=NOW
    )
    assert await store.read_companion_context(2, "bob", "u1") is None
    assert await store.read_companion_context(1, "alice", "u1") is None
    assert (await store.read_companion_context(1, "bob", "u1"))["life"]["focus"] == "Bob hobby"
    with pytest.raises(CompanionConflict):
        await store.set_mode(2, "companion", persona_key="bob", now=NOW)


@pytest.mark.asyncio
async def test_real_chat_is_idempotent_neutral_feedback_and_late_review_loses(companion_db):
    member = await enable_member(companion_db)
    before = await store.get_mode(1)
    review = await store.claim_review(1, "alice", now=NOW)
    native_id = await turn(companion_db, at=NOW)
    changed = await store.record_chat(
        1,
        "alice",
        user_id="u1",
        platform_user_id="p1",
        display_name="Updated",
        turn_id=native_id,
        now=NOW,
    )
    after = await store.get_mode(1)
    replay = await store.record_chat(
        1,
        "alice",
        user_id="u1",
        platform_user_id="p1",
        display_name="Ignored replay",
        turn_id=native_id,
        now=NOW + timedelta(minutes=1),
    )
    assert changed == replay
    assert changed.version == member.version + 1
    assert changed.last_turn_id == native_id
    assert changed.last_user_at == NOW
    assert after.context_revision == before.context_revision + 1
    assert await store.get_mode(1) == after
    assert not await store.finish_review(
        review, ReflectionProposal(life=LifeState(focus="Stale")), now=NOW, next_review_at=NOW
    )
    assert not await store.fail_review(review, now=NOW, retry_at=NOW + timedelta(minutes=1), reason="late_interruption")
    grace_due = NOW + timedelta(minutes=5)
    before_grace = grace_due - timedelta(seconds=1)
    assert await store.due_scopes(now=before_grace) == ()
    assert await store.claim_review(1, "alice", now=before_grace) is None
    assert tuple(mode.scope_id for mode in await store.due_scopes(now=grace_due)) == (1,)
    replacement = await store.claim_review(1, "alice", now=grace_due)
    assert replacement is not None
    snapshot = await store.load_reflection(replacement, now=grace_due)
    assert snapshot.members == (changed,)
    assert set(snapshot.member_turn_ids[changed.member_ref]) == {native_id, member.last_turn_id}
    async with companion_db.factory() as db:
        relation = await db.get(UserRelation, ("u1", "group1"))
        assert (relation.affection, relation.trust) == (42.0, 53.0)
        assert await db.scalar(select(func.count()).select_from(CompanionChatEvidence)) == 2
        assert (await db.get(CompanionReview, review.lease_token)).status == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("scheduled_minutes", [60, 180])
async def test_completed_chats_preserve_later_review_without_spending_reservations(companion_db, scheduled_minutes):
    member = await enable_member(companion_db)
    first = await store.claim_review(1, "alice", now=NOW)
    scheduled_at = NOW + timedelta(minutes=scheduled_minutes)
    assert await store.finish_review(first, ReflectionProposal(life=LifeState()), now=NOW, next_review_at=scheduled_at)
    before = await store.get_mode(1)
    turn_ids = [member.last_turn_id]
    for minute in (1, 10, 30):
        chatted_at = NOW + timedelta(minutes=minute)
        native_id = await turn(companion_db, at=chatted_at)
        turn_ids.append(native_id)
        updated = await store.record_chat(
            1, "alice", user_id="u1", platform_user_id="p1", display_name="One", turn_id=native_id, now=chatted_at
        )
        assert updated.last_turn_id == native_id
        assert (await store.get_mode(1)).next_review_at == scheduled_at
        quiet_at = chatted_at + timedelta(minutes=5)
        assert await store.due_scopes(now=quiet_at) == ()
        assert await store.claim_review(1, "alice", now=quiet_at) is None
    after = await store.get_mode(1)
    assert after.context_revision == before.context_revision + 3
    assert updated.version == member.version + 3
    assert tuple(mode.scope_id for mode in await store.due_scopes(now=scheduled_at)) == (1,)
    next_claim = await store.claim_review(1, "alice", now=scheduled_at)
    assert next_claim is not None
    snapshot = await store.load_reflection(next_claim, now=scheduled_at)
    assert set(snapshot.member_turn_ids[member.member_ref]) == set(turn_ids)
    async with companion_db.factory() as db:
        assert await db.scalar(select(func.count()).select_from(CompanionReview)) == 2
        assert await db.scalar(select(func.count()).select_from(CompanionChatEvidence)) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("scheduled_minutes", [None, -30, 0, 2])
async def test_completed_chat_defers_unscheduled_or_near_due_review_through_quiet_grace(
    companion_db, scheduled_minutes
):
    await enable_member(companion_db)
    async with companion_db.factory() as db, db.begin():
        mode = await db.get(CompanionMode, 1)
        mode.next_review_at = (
            (NOW + timedelta(minutes=scheduled_minutes)).replace(tzinfo=None) if scheduled_minutes is not None else None
        )
    native_id = await turn(companion_db)
    await store.record_chat(
        1, "alice", user_id="u1", platform_user_id="p1", display_name="One", turn_id=native_id, now=NOW
    )
    grace_due = NOW + timedelta(minutes=5)
    before_grace = grace_due - timedelta(seconds=1)
    assert await store.due_scopes(now=before_grace) == ()
    assert await store.claim_review(1, "alice", now=before_grace) is None
    assert tuple(mode.scope_id for mode in await store.due_scopes(now=grace_due)) == (1,)
    assert await store.claim_review(1, "alice", now=grace_due) is not None


@pytest.mark.asyncio
async def test_registered_actor_has_no_fabricated_completed_turn(companion_db):
    member = await enable_member(companion_db, real=False)
    assert member.last_turn_id == 0
    assert member.version == 0
    claim = await store.claim_review(1, "alice", now=NOW)
    snapshot = await store.load_reflection(claim, now=NOW)
    assert snapshot.member_turn_ids[member.member_ref] == ()
    with pytest.raises(TypeError):
        snapshot.member_turn_ids["new"] = (1,)
    with pytest.raises(CompanionConflict):
        await store.finish_review(
            claim,
            ReflectionProposal(life=LifeState(), contact=proposal(member, purpose="check_in")),
            now=NOW,
            next_review_at=NOW + timedelta(hours=1),
        )
    assert await contact(companion_db, member) is not None


@pytest.mark.asyncio
async def test_review_contention_budget_and_sparse_rearm_without_contact(companion_db):
    await store.set_mode(1, "companion", persona_key="alice", now=NOW)
    claims = await asyncio.gather(*[store.claim_review(1, "alice", now=NOW, daily_limit=1) for _ in range(4)])
    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    assert await store.finish_review(
        winners[0], ReflectionProposal(life=LifeState()), now=NOW, next_review_at=NOW + timedelta(hours=2)
    )
    assert (await store.get_mode(1)).next_review_at == NOW + timedelta(hours=2)
    assert await store.claim_review(1, "alice", now=NOW + timedelta(hours=2), daily_limit=1) is None
    mode = await store.get_mode(1)
    assert mode.mode == "companion"
    assert mode.next_review_at == datetime(2026, 10, 10, 16, tzinfo=UTC)
    for hour in (3, 6):
        chatted_at = NOW + timedelta(hours=hour)
        native_id = await turn(companion_db, at=chatted_at)
        await store.record_chat(
            1, "alice", user_id="u1", platform_user_id="p1", display_name="One", turn_id=native_id, now=chatted_at
        )
        assert (await store.get_mode(1)).next_review_at == mode.next_review_at
        quiet_at = chatted_at + timedelta(minutes=5)
        assert await store.due_scopes(now=quiet_at) == ()
        assert await store.claim_review(1, "alice", now=quiet_at, daily_limit=1) is None
    async with companion_db.factory() as db:
        assert await db.scalar(select(func.count()).select_from(CompanionReview)) == 1
        assert await db.scalar(select(func.count()).select_from(CompanionChatEvidence)) == 2
    next_claim = await store.claim_review(1, "alice", now=mode.next_review_at, daily_limit=1)
    assert next_claim is not None
    assert await store.fail_review(
        next_claim,
        now=mode.next_review_at,
        retry_at=mode.next_review_at + timedelta(minutes=5),
        reason="provider_failed",
    )
    assert (await store.get_mode(1)).mode == "companion"


@pytest.mark.asyncio
async def test_contact_contention_exact_targets_and_preferences(companion_db):
    member = await enable_member(companion_db)
    p = proposal(member)
    origin = await review_for_contact(companion_db, member, p)
    claims = await asyncio.gather(
        *[
            store.claim_contact(1, "alice", member.member_ref, p, origin=origin, now=NOW, limits=LIMITS)
            for _ in range(3)
        ]
    )
    assert len([claim for claim in claims if claim is not None]) == 1
    winner = next(claim for claim in claims if claim)
    assert winner.user_id == "u1"
    assert winner.platform_user_id == "p1"
    assert not await store.contact_valid(replace(winner, platform_user_id="p2"), now=NOW)
    assert await store.finish_contact(winner, status="cancelled", now=NOW)
    async with companion_db.factory() as db, db.begin():
        db.add(ReminderPreference(account_id="bot", unified_user_id="u1", enabled=False))
    assert await store.read_members(1, "alice", now=NOW) == ()
    with pytest.raises(CompanionConflict):
        await review_for_contact(companion_db, member, proposal(member, "other"))
    async with companion_db.factory() as db:
        assert (
            await db.scalar(select(ReminderTask).where(ReminderTask.task_ref == "existing-reminder"))
        ).status == "active"


@pytest.mark.asyncio
async def test_quiet_grace_idle_and_active_day_budget(companion_db):
    member = await enable_member(companion_db)
    assert await contact(companion_db, member, at=NOW + timedelta(hours=13)) is None
    assert await contact(companion_db, member, at=member.last_user_at + timedelta(minutes=4)) is None
    assert await contact(companion_db, member, at=NOW + timedelta(days=15)) is None
    claim = await contact(companion_db, member, limits=replace(LIMITS, daily_contacts=1))
    assert claim is not None
    second = await enable_member(companion_db, user="u2", platform_user="p2")
    # A new real member invalidates the old unstarted attempt; reserve it again.
    first = await contact(companion_db, member, key="reserved", limits=replace(LIMITS, daily_contacts=1))
    assert first is not None
    assert await contact(companion_db, second, key="second", limits=replace(LIMITS, daily_contacts=1)) is not None
    assert await contact(companion_db, member, key="same-user", limits=replace(LIMITS, daily_contacts=1)) is None


@pytest.mark.asyncio
async def test_off_during_transport_keeps_receipts_and_cannot_send_again(companion_db):
    member = await enable_member(companion_db)
    claim = await contact(companion_db, member)
    assert await store.record_transport_started(claim, now=NOW)
    await store.set_mode(1, "normal", persona_key="alice", now=NOW + timedelta(seconds=1))
    assert not await store.record_transport_started(claim, now=NOW + timedelta(seconds=2))
    await store.record_receipts(claim, ["real-1", "real-2"], now=NOW + timedelta(seconds=3))
    await store.record_receipts(claim, ["real-1", "real-2"], now=NOW + timedelta(seconds=4))
    assert await store.finish_contact(claim, status="delivered", now=NOW + timedelta(seconds=5))
    history = await store.read_contact_history(1, "alice")
    assert history[0]["receipts"] == ("real-1", "real-2")
    assert history[0]["status"] == "delivered"
    with pytest.raises(TypeError):
        history[0]["status"] = "failed"
    try:
        await store.record_receipts(claim, [""], now=NOW)
    except ValueError:
        pass
    else:
        pytest.fail("An empty protocol receipt was accepted")


@pytest.mark.asyncio
async def test_unknown_prefix_recovery_is_terminal_and_unknown_key_never_replayed(companion_db):
    member = await enable_member(companion_db)
    claim = await contact(companion_db, member, lease_seconds=60)
    assert await store.record_transport_started(claim, now=NOW)
    await store.record_receipts(claim, ["first"], now=NOW + timedelta(seconds=1))
    assert await store.record_transport_started(claim, now=NOW + timedelta(seconds=2))
    assert await store.recover_stale(now=NOW + timedelta(minutes=2)) == 1
    history = await store.read_contact_history(1, "alice")
    assert history[0]["status"] == "unknown"
    assert history[0]["receipts"] == ("first",)
    await store.initialize_store(now=NOW + timedelta(minutes=3))
    assert not await store.record_transport_started(claim, now=NOW + timedelta(minutes=3))
    review_at = NOW + timedelta(hours=1)
    review = await store.claim_review(1, "alice", now=review_at)
    assert review is not None
    assert await store.finish_review(
        review,
        ReflectionProposal(life=LifeState(focus="A new activity")),
        now=review_at,
        next_review_at=review_at + timedelta(hours=1),
    )
    # Later reflection epochs do not authorize replay of the unknown same motive.
    assert await contact(companion_db, member, at=NOW + timedelta(days=1), key="chat-1") is None
    assert await contact(companion_db, member, at=NOW + timedelta(days=1), key="distinct-new-motive") is not None


@pytest.mark.asyncio
async def test_fixed_contact_debounce_and_actual_new_chat_reset(companion_db):
    member = await enable_member(companion_db)
    first = await contact(companion_db, member)
    assert await store.record_transport_started(first, now=NOW)
    await store.record_receipts(first, ["real"], now=NOW)
    assert await store.finish_contact(first, status="delivered", now=NOW)
    assert await contact(companion_db, member, at=NOW + timedelta(minutes=4), key="too-soon") is None
    second = await contact(companion_db, member, at=NOW + timedelta(minutes=5), key="different")
    assert second is not None
    assert await store.record_transport_started(second, now=NOW + timedelta(minutes=5))
    await store.record_receipts(second, ["real-2"], now=NOW + timedelta(minutes=5))
    assert await store.finish_contact(second, status="delivered", now=NOW + timedelta(minutes=5))
    chat_time = NOW + timedelta(hours=6, minutes=1)
    native = await turn(companion_db, at=chat_time)
    updated = await store.record_chat(
        1, "alice", user_id="u1", platform_user_id="p1", display_name="u1", turn_id=native, now=chat_time
    )
    assert await contact(companion_db, updated, at=chat_time + timedelta(minutes=5), key="after-real-chat") is not None


@pytest.mark.asyncio
async def test_attention_grounding_and_confirmed_contact_waits_for_real_reply(companion_db):
    member = await enable_member(companion_db)
    other = await enable_member(companion_db, user="u2", platform_user="p2")
    review = await store.claim_review(1, "alice", now=NOW)
    cross_member = ReflectionProposal(
        life=LifeState(focus="Must roll back"),
        attention=[
            AttentionUpdate(
                member_ref=member.member_ref,
                key="concern",
                kind="concern",
                summary="An ongoing concern",
                status="open",
                evidence_turn_ids=[other.last_turn_id],
            )
        ],
    )
    with pytest.raises(CompanionConflict):
        await store.finish_review(review, cross_member, now=NOW, next_review_at=NOW)
    assert (await store.load_reflection(review, now=NOW)).life.focus == ""
    p = proposal(member, "concern", purpose="check_in", evidence=[member.last_turn_id])
    grounded = ReflectionProposal(
        life=LifeState(),
        attention=[
            AttentionUpdate(
                member_ref=member.member_ref,
                key="concern",
                kind="concern",
                summary="An ongoing concern",
                status="open",
                evidence_turn_ids=[member.last_turn_id],
            )
        ],
        contact=p,
    )
    assert await store.finish_review(review, grounded, now=NOW, next_review_at=NOW + timedelta(minutes=10))
    check_in = await store.claim_contact(1, "alice", member.member_ref, p, origin=review, now=NOW, limits=LIMITS)
    native = await turn(companion_db, purpose="companion", trigger="autonomous", status="running")
    assert await store.bind_contact_turn(check_in, native, now=NOW)
    assert await store.record_transport_started(check_in, now=NOW)
    await store.record_receipts(check_in, ["confirmed"], now=NOW)
    assert await store.finish_contact(check_in, status="delivered", now=NOW)
    own = await store.read_companion_context(1, "alice", "u1")
    assert own["attention"][0]["status"] == "waiting_reply"
    assert (await store.read_companion_context(1, "alice", "u2"))["attention"] == []
    native_reply = await turn(companion_db, at=NOW + timedelta(minutes=1))
    await store.record_chat(
        1,
        "alice",
        user_id="u1",
        platform_user_id="p1",
        display_name="u1",
        turn_id=native_reply,
        now=NOW + timedelta(minutes=1),
    )
    assert (await store.read_companion_context(1, "alice", "u1"))["attention"][0]["status"] == "waiting_reply"


@pytest.mark.asyncio
async def test_reflection_attention_is_a_merge_so_omitted_items_stay_unchanged(companion_db):
    # The reflection prompt asks for attention changes only; replacing state would erase concerns.
    member = await enable_member(companion_db)
    concern = AttentionUpdate(
        member_ref=member.member_ref,
        key="exam",
        kind="concern",
        summary="Preparing for an exam",
        status="open",
        evidence_turn_ids=[member.last_turn_id],
    )
    first = await store.claim_review(1, "alice", now=NOW)
    assert await store.finish_review(
        first,
        ReflectionProposal(life=LifeState(), attention=[concern]),
        now=NOW,
        next_review_at=NOW + timedelta(minutes=10),
    )
    later = NOW + timedelta(minutes=10)
    second = await store.claim_review(1, "alice", now=later)
    topic = concern.model_copy(update={"key": "music", "kind": "topic", "summary": "Shares new songs"})
    assert await store.finish_review(
        second,
        ReflectionProposal(life=LifeState(), attention=[topic]),
        now=later,
        next_review_at=later + timedelta(minutes=10),
    )

    attention = {item["key"]: item for item in (await store.read_companion_context(1, "alice", "u1"))["attention"]}
    assert attention["exam"]["status"] == "open"
    assert attention["exam"]["summary"] == "Preparing for an exam"
    assert attention["music"]["kind"] == "topic"


@pytest.mark.asyncio
async def test_corrupt_generated_state_is_explicit_and_failed_write_rolls_back(companion_db):
    member = await enable_member(companion_db)
    async with companion_db.factory() as db, db.begin():
        db.add(
            CompanionLife(
                scope_id=1,
                persona_key="alice",
                version=0,
                state_json='{"unexpected":true}',
                updated_at=NOW.replace(tzinfo=None),
            )
        )
    with pytest.raises(CompanionCorruptState):
        await store.read_companion_context(1, "alice", "u1")
    with pytest.raises(CompanionCorruptState):
        await store.claim_review(1, "alice", now=NOW)
    with pytest.raises(CompanionConflict):
        await store.register_member(1, "alice", user_id="other", platform_user_id="p1", display_name="Other", now=NOW)
    async with companion_db.factory() as db:
        assert await db.scalar(select(func.count()).select_from(CompanionReview)) == 0
        assert (await db.get(CompanionMode, 1)).context_revision == 1
        assert (await store.read_members(1, "alice", now=NOW))[0].member_ref == member.member_ref


@pytest.mark.asyncio
async def test_same_free_chat_can_repeat_only_after_real_state_change(companion_db):
    member = await enable_member(companion_db)
    claim = await contact(companion_db, member)
    assert await store.record_transport_started(claim, now=NOW)
    await store.record_receipts(claim, ["initial"], now=NOW)
    assert await store.finish_contact(claim, status="delivered", now=NOW)
    later = NOW + timedelta(days=1)
    review = await store.claim_review(1, "alice", now=later)
    assert await store.finish_review(
        review, ReflectionProposal(life=LifeState()), now=later, next_review_at=later + timedelta(minutes=10)
    )
    assert await contact(companion_db, member, at=later) is None
    changed_at = later + timedelta(hours=1)
    review = await store.claim_review(1, "alice", now=changed_at)
    assert await store.finish_review(
        review,
        ReflectionProposal(life=LifeState(focus="Finished a sketch")),
        now=changed_at,
        next_review_at=changed_at + timedelta(hours=1),
    )
    assert await contact(companion_db, member, at=changed_at) is not None


@pytest.mark.asyncio
async def test_fictional_continuity_survives_sparse_omission_and_snapshots_are_immutable(companion_db):
    await store.set_mode(1, "companion", persona_key="alice", now=NOW)
    first = LifeState(
        plans=[LifePlan(key="sketch", title="Finish a sketch", status="active", progress="Started")],
        recent_events=[LifeEvent(key="walk", summary="A fictional walk in the garden")],
    )
    claim = await store.claim_review(1, "alice", now=NOW)
    assert await store.finish_review(
        claim, ReflectionProposal(life=first), now=NOW, next_review_at=NOW + timedelta(minutes=10)
    )
    second_at = NOW + timedelta(minutes=10)
    second = await store.claim_review(1, "alice", now=second_at)
    snapshot = await store.load_reflection(second, now=second_at)
    with pytest.raises(TypeError):
        snapshot.life.plans.append(LifePlan(key="injected", title="No", status="planned", progress=""))
    with pytest.raises(CompanionConflict):
        await store.finish_review(second, ReflectionProposal(life=LifeState()), now=second_at, next_review_at=second_at)
    completed = LifeState(
        plans=[LifePlan(key="sketch", title="Finish a sketch", status="completed", progress="Finished")],
        recent_events=list(first.recent_events),
    )
    assert await store.finish_review(
        second, ReflectionProposal(life=completed), now=second_at, next_review_at=second_at + timedelta(minutes=10)
    )
    third_at = second_at + timedelta(minutes=10)
    third = await store.claim_review(1, "alice", now=third_at)
    assert await store.finish_review(
        third, ReflectionProposal(life=LifeState()), now=third_at, next_review_at=third_at + timedelta(minutes=10)
    )
    fourth_at = third_at + timedelta(minutes=10)
    fourth = await store.claim_review(1, "alice", now=fourth_at)
    with pytest.raises(CompanionConflict):
        await store.finish_review(fourth, ReflectionProposal(life=first), now=fourth_at, next_review_at=fourth_at)
    rewritten = LifeState(recent_events=[LifeEvent(key="walk", summary="A different past event")])
    with pytest.raises(CompanionConflict):
        await store.finish_review(fourth, ReflectionProposal(life=rewritten), now=fourth_at, next_review_at=fourth_at)


@pytest.mark.asyncio
async def test_unknown_started_contact_consumes_only_its_user_budget_after_restart(companion_db):
    member = await enable_member(companion_db)
    claim = await contact(companion_db, member, limits=replace(LIMITS, daily_contacts=1), lease_seconds=60)
    assert await store.record_transport_started(claim, now=NOW)
    await store.initialize_store(now=NOW + timedelta(minutes=2))
    second = await enable_member(companion_db, user="u2", platform_user="p2", at=NOW + timedelta(minutes=3))
    assert (
        await contact(
            companion_db,
            second,
            at=NOW + timedelta(minutes=10),
            limits=replace(LIMITS, daily_contacts=1),
            key="new-user",
        )
        is not None
    )
    assert (
        await contact(
            companion_db,
            member,
            at=NOW + timedelta(minutes=10),
            limits=replace(LIMITS, daily_contacts=1),
            key="same-user",
        )
        is None
    )
    assert (await store.read_contact_history(1, "alice", member_ref=member.member_ref))[0]["status"] == "unknown"


@pytest.mark.asyncio
async def test_native_turn_scope_sender_and_purpose_are_not_fabricated(companion_db):
    member = await enable_member(companion_db)
    foreign = await turn(companion_db, scope=2, user="u1")
    with pytest.raises(CompanionConflict):
        await store.record_chat(
            1, "alice", user_id="u1", platform_user_id="p1", display_name="u1", turn_id=foreign, now=NOW
        )
    scheduled = await turn(companion_db, purpose="companion", trigger="autonomous", status="completed")
    with pytest.raises(CompanionConflict):
        await store.record_chat(
            1, "alice", user_id="u1", platform_user_id="p1", display_name="u1", turn_id=scheduled, now=NOW
        )
    p = proposal(member)
    origin = await review_for_contact(companion_db, member, p)
    claim = await store.claim_contact(1, "alice", member.member_ref, p, origin=origin, now=NOW, limits=LIMITS)
    with pytest.raises(CompanionConflict):
        await store.bind_contact_turn(claim, foreign, now=NOW)
    wrong_sender = await turn(companion_db, user="u2", purpose="companion", trigger="autonomous", status="running")
    with pytest.raises(CompanionConflict):
        await store.bind_contact_turn(claim, wrong_sender, now=NOW)
    with pytest.raises(CompanionConflict):
        await store.record_transport_started(claim, now=NOW)


@pytest.mark.asyncio
async def test_renewal_and_each_send_recheck_preferences_and_revisions(companion_db):
    member = await enable_member(companion_db)
    claim = await contact(companion_db, member, lease_seconds=30)
    assert await store.renew_contact(claim, now=NOW + timedelta(seconds=20), lease_seconds=300)
    assert await store.record_transport_started(claim, now=NOW + timedelta(seconds=40))
    await store.record_receipts(claim, ["prefix"], now=NOW + timedelta(seconds=41))
    async with companion_db.factory() as db, db.begin():
        db.add(ReminderPreference(account_id="bot", unified_user_id="u1", enabled=False))
    assert not await store.record_transport_started(claim, now=NOW + timedelta(seconds=42))
    assert not await store.renew_contact(claim, now=NOW + timedelta(seconds=42))
    assert await store.finish_contact(claim, status="cancelled", now=NOW + timedelta(seconds=43))
    assert (await store.read_contact_history(1, "alice"))[0]["receipts"] == ("prefix",)


@pytest.mark.asyncio
async def test_expired_review_reservation_is_fenced_and_retained_for_budget(companion_db):
    await store.set_mode(1, "companion", persona_key="alice", now=NOW)
    first = await store.claim_review(1, "alice", now=NOW, lease_seconds=30, daily_limit=2)
    assert await store.recover_stale(now=NOW + timedelta(seconds=31)) == 1
    second = await store.claim_review(1, "alice", now=NOW + timedelta(seconds=31), daily_limit=2)
    assert second is not None
    assert first.lease_token != second.lease_token
    assert not await store.finish_review(
        first, ReflectionProposal(life=LifeState(focus="Late")), now=NOW + timedelta(seconds=32), next_review_at=NOW
    )
    assert not await store.fail_review(first, now=NOW + timedelta(seconds=32), retry_at=NOW, reason="late")
    assert await store.finish_review(
        second,
        ReflectionProposal(life=LifeState(focus="Current")),
        now=NOW + timedelta(seconds=32),
        next_review_at=NOW + timedelta(minutes=11),
    )
    assert await store.claim_review(1, "alice", now=NOW + timedelta(minutes=11), daily_limit=2) is None


@pytest.mark.asyncio
async def test_off_on_cannot_reauthorize_unreserved_old_motive(companion_db):
    member = await enable_member(companion_db)
    p = proposal(member)
    origin = await review_for_contact(companion_db, member, p)
    await store.set_mode(1, "normal", persona_key="alice", now=NOW)
    await store.set_mode(1, "companion", persona_key="alice", now=NOW)
    assert await store.claim_contact(1, "alice", member.member_ref, p, origin=origin, now=NOW, limits=LIMITS) is None
    fresh = await review_for_contact(companion_db, member, p)
    changed = p.model_copy(update={"topic": "An unapproved replacement topic"})
    with pytest.raises(CompanionConflict):
        await store.claim_contact(1, "alice", member.member_ref, changed, origin=fresh, now=NOW, limits=LIMITS)


@pytest.mark.asyncio
async def test_persona_switch_preserves_fixed_same_user_cooldown(companion_db):
    member = await enable_member(companion_db)
    first = await contact(companion_db, member)
    assert await store.record_transport_started(first, now=NOW)
    await store.record_receipts(first, ["confirmed-alice"], now=NOW)
    assert await store.finish_contact(first, status="delivered", now=NOW)
    async with companion_db.factory() as db, db.begin():
        (await db.get(ScopePersonaSelection, 1)).persona_key = "bob"
    await store.rebind_persona(1, "bob", now=NOW + timedelta(minutes=1))
    second = await store.register_member(
        1,
        "bob",
        user_id="u1",
        platform_user_id="p1",
        display_name="u1",
        now=NOW - timedelta(minutes=10),
    )
    assert await contact(companion_db, second, at=NOW + timedelta(minutes=4), key="bob-too-soon") is None
    assert await contact(companion_db, second, at=NOW + timedelta(minutes=5), key="bob-greeting") is not None
