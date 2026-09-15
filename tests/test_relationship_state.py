"""Relationship evidence transactions, recovery and member isolation contracts."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
import asyncio
from pathlib import Path

import pytest
from sqlalchemy import text, select, update
from arclet.entari.config import EntariConfig

if not hasattr(EntariConfig, "instance"):
    setattr(EntariConfig, "instance", EntariConfig.load(Path(__file__).resolve().parents[1] / "entari.yml"))

from entari_plugin_database import Base
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from plugins.llm_chat import identity, agent_events, chat_evaluation, session_manager
from plugins.llm_chat.config import LLMChatConfig
from plugins.llm_chat.models import AgentTurn, AgentEvent, UserAffect, UserMemory, UserRelation, RelationshipEvidence
from plugins.llm_chat.persona import store as persona_store, memory_update, memory_context
from plugins.llm_chat.core.eval import EvalResult, parse_eval_response
from plugins.llm_chat.core.profile import MemoryItem
from plugins.llm_chat.relationships import state, evidence, recovery, migration, evaluation_store
from utils.relationship_core.models import AXIS_KEYS
from utils.relationship_core.policy import read_emotions
from plugins.llm_chat.session_manager import ScopeIdentity, BaselineFingerprint
from plugins.llm_chat.core.agent_trace import AgentTurnRecorder
from plugins.llm_chat.relationships.types import EvaluationBatch, RelationshipConflict


@pytest.fixture
async def relationship_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'relationships.db'}", connect_args={"timeout": 15})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    for module in (
        agent_events,
        session_manager,
        persona_store,
        state,
        evidence,
        recovery,
        migration,
        evaluation_store,
        memory_context,
        memory_update,
        identity,
    ):
        monkeypatch.setattr(module, "get_session", factory)
    monkeypatch.setattr(chat_evaluation, "_STOPPING", False)
    try:
        yield SimpleNamespace(engine=engine, factory=factory)
    finally:
        await chat_evaluation.cancel_pending_evaluations()
        await engine.dispose()


async def _turn(
    user: str,
    content: str,
    *,
    status: str = "completed",
    channel: str = "channel",
    persona: str = "A thoughtful conversational character.",
    decision: bool = True,
):
    await persona_store.get_relation(user, channel)
    scope = await session_manager.get_or_create_scope(ScopeIdentity("test", "bot", "guild", channel, channel))
    session = await session_manager.get_active_session(scope.id)
    if session is None:
        baseline = BaselineFingerprint("test", "persona", "system", "tools", "policy")
        session = await session_manager.create_session(scope.id, baseline, start_reason="test")
    turn = await session_manager.start_turn(
        session,
        trigger_message_id=f"message-{user}-{content}",
        user_id=user,
        user_name=user,
        conversation_user_id=None,
        fresh_context=False,
    )
    recorder = AgentTurnRecorder()
    recorder.record_user_input(json.dumps({"speaker": user, "content": content}), user_name=user, fresh_context=False)
    recorder.append("context_snapshot", payload={"persona": {"prompt": persona}}, model_visible=False)
    reply = "I understand." if status in {"completed", "declined"} else ""
    if reply:
        recorder.record_assistant_output(reply)
    if decision:
        recorder.append(
            "response_decision",
            model_visible=False,
            payload={
                "outcome": "automatic" if status == "completed" else status,
                "source": "model",
                "reason": "",
                "actual_delivery": {
                    "text_messages": int(bool(reply)),
                    "media_messages": 0,
                    "confirmed_deliveries": int(bool(reply)),
                },
            },
        )
    await agent_events.persist_agent_events(turn.id, recorder.events)
    await session_manager.finish_turn(turn.id, status=status, final_text=reply)
    return turn


async def _enqueue(turn, *, persona: str = "A thoughtful conversational character.", channel: str = "channel"):
    return await evidence.enqueue_relationship_evidence(
        turn_id=turn.id,
        user_id=turn.user_id,
        channel_id=channel,
        persona_prompt=persona,
        model="test",
    )


def _result(batch: EvaluationBatch, trust: float = 4.0):
    payload = {
        "deltas": {name: trust if name == "trust" else 0.0 for name in AXIS_KEYS},
        "description": "Repeated reliable interaction makes this person easier to trust.",
        "impression": "Reliable and attentive.",
        "emotions": [],
        "processed_turn_ids": list(batch.turn_ids),
        "profile_patches": [],
        "memory_items": [],
    }
    result = parse_eval_response(
        json.dumps(payload),
        expected_turn_ids=batch.turn_ids,
        previous_emotions=read_emotions(batch.before["emotions"]),
        now=time.time(),
    )
    assert result is not None
    return result


async def _processed(factory, count: int) -> None:
    async def wait_for_rows():
        while True:
            async with factory() as db:
                rows = list((await db.execute(select(RelationshipEvidence))).scalars())
            if len(rows) == count and all(row.status == "processed" for row in rows):
                return
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait_for_rows(), timeout=10)


@pytest.mark.asyncio
async def test_complete_batch_is_consumed_once_without_cross_member_scoring(relationship_store):
    first = await _turn("alice", "I stayed to help when everyone else left.")
    second = await _turn("alice", "Hello again.")
    other = await _turn("bob", "An unrelated message.")
    for turn in (first, second, other):
        assert await _enqueue(turn)
    assert not await _enqueue(first)
    batch = await evaluation_store.claim_evaluation("alice", "channel", batch_size=8, model="test")
    assert batch is not None
    assert batch.turn_ids == (first.id, second.id)
    after = await evaluation_store.commit_evaluation(batch, _result(batch))
    assert after["axes"]["trust"] == 34.0
    assert await evaluation_store.claim_evaluation("alice", "channel", batch_size=8, model="test") is None
    with pytest.raises(RelationshipConflict):
        await evaluation_store.commit_evaluation(batch, _result(batch))
    async with relationship_store.factory() as db:
        alice = await db.get(UserRelation, ("alice", "channel"))
        bob = await db.get(UserRelation, ("bob", "channel"))
        jobs = list((await db.execute(select(RelationshipEvidence).order_by(RelationshipEvidence.turn_id))).scalars())
        events = list(
            (await db.execute(select(AgentEvent).where(AgentEvent.event_type == "relationship_evaluation"))).scalars()
        )
    assert alice.trust == 34.0
    assert bob.trust == 30.0
    assert [row.status for row in jobs] == ["processed", "processed", "pending"]
    evaluated = [json.loads(event.payload_json) for event in events if event.status == "succeeded"]
    assert all(item["evidence_turn_ids"] == [first.id, second.id] for item in evaluated)
    assert {item["evaluation_ref"] for item in evaluated} == {batch.evaluation_ref}
    assert all(item["changes"]["trust"] == {"before": 30.0, "after": 34.0, "delta": 4.0} for item in evaluated)


@pytest.mark.asyncio
async def test_stale_evaluator_cannot_overwrite_a_newer_relationship(relationship_store):
    turn = await _turn("alice", "An interaction awaiting evaluation.")
    await _enqueue(turn)
    old = await evaluation_store.claim_evaluation("alice", "channel", batch_size=8, model="test")
    assert old is not None
    async with relationship_store.factory() as db:
        await db.execute(update(UserAffect).where(UserAffect.user_id == "alice").values(version=UserAffect.version + 1))
        await db.execute(update(UserRelation).where(UserRelation.user_id == "alice").values(trust=80.0))
        await db.commit()
    with pytest.raises(RelationshipConflict):
        await evaluation_store.commit_evaluation(old, _result(old, trust=-20.0))
    await evaluation_store.release_evaluation(old, "State superseded")
    replacement = await evaluation_store.claim_evaluation("alice", "channel", batch_size=8, model="test")
    assert replacement is not None
    await evaluation_store.commit_evaluation(replacement, _result(replacement))
    async with relationship_store.factory() as db:
        relation = await db.get(UserRelation, ("alice", "channel"))
        event = await db.get(AgentEvent, replacement.event_ids[0])
    assert relation.trust == 84.0
    assert event.status == "succeeded"
    assert json.loads(event.payload_json)["before"]["axes"]["trust"] == 80.0


@pytest.mark.asyncio
async def test_cancelled_worker_restores_evidence_including_newer_interactions(
    relationship_store,
    monkeypatch: pytest.MonkeyPatch,
):
    config = LLMChatConfig(memory_enabled=False, relationship_eval_debounce_seconds=0)
    entered = asyncio.Event()

    async def waiting_evaluator(*_args):
        entered.set()
        await asyncio.Future()

    monkeypatch.setattr(chat_evaluation, "run_evaluation", waiting_evaluator)
    first = await _turn("alice", "A meaningful earlier event.")
    chat_evaluation.schedule_relationship_evaluation(
        config,
        turn_id=first.id,
        user_id="alice",
        channel_id="channel",
        persona_prompt="Persona",
        warn=lambda _: None,
    )
    await asyncio.wait_for(entered.wait(), timeout=10)
    second = await _turn("alice", "A newer event while the evaluator is waiting.", persona="Persona")
    await _enqueue(second, persona="Persona")
    await chat_evaluation.cancel_pending_evaluations()
    async with relationship_store.factory() as db:
        rows = list((await db.execute(select(RelationshipEvidence).order_by(RelationshipEvidence.turn_id))).scalars())
        relation = await db.get(UserRelation, ("alice", "channel"))
    assert [(row.turn_id, row.status) for row in rows] == [(first.id, "pending"), (second.id, "pending")]
    assert relation.trust == 30.0
    seen: list[tuple[int, ...]] = []

    async def evaluator(_config, _persona, relationship, _facts, episodes, _channel):
        ids = tuple(episode["turn_id"] for episode in episodes)
        seen.append(ids)
        payload = {
            "deltas": {name: 3.0 if name == "trust" else 0.0 for name in AXIS_KEYS},
            "description": "Both pending interactions contribute to the updated relationship.",
            "impression": "An attentive conversation partner.",
            "emotions": [],
            "processed_turn_ids": list(ids),
            "profile_patches": [],
            "memory_items": [],
        }
        return parse_eval_response(json.dumps(payload), expected_turn_ids=ids, now=time.time())

    monkeypatch.setattr(chat_evaluation, "run_evaluation", evaluator)
    await chat_evaluation.resume_relationship_evaluations(config, lambda _: None)
    await _processed(relationship_store.factory, 2)
    assert seen == [(first.id, second.id)]
    async with relationship_store.factory() as db:
        relation = await db.get(UserRelation, ("alice", "channel"))
    assert relation.trust == 33.0


@pytest.mark.asyncio
async def test_schema_cutover_keeps_relationships_and_only_recovers_post_cutover_turns(relationship_store):
    old = await _turn("alice", "Historical already-accounted interaction.")
    async with relationship_store.factory() as db:
        await db.execute(update(UserRelation).where(UserRelation.user_id == "alice").values(trust=77.0, affection=81.0))
        await db.execute(text("ALTER TABLE chat_user_relations ADD COLUMN eval_counter INTEGER NOT NULL DEFAULT 3"))
        await db.commit()
    await migration.initialize_relationship_store()
    await persona_store.get_relation("new-member", "channel")
    fresh = await _turn("alice", "Committed after migration but before enqueue.")
    assert await recovery.recover_unqueued_evidence("test") == 1
    assert await recovery.recover_unqueued_evidence("test") == 0
    async with relationship_store.factory() as db:
        relation = await db.get(UserRelation, ("alice", "channel"))
        queued = list((await db.execute(select(RelationshipEvidence.turn_id))).scalars())
    assert (relation.trust, relation.affection) == (77.0, 81.0)
    assert queued == [fresh.id]
    assert old.id not in queued


@pytest.mark.asyncio
async def test_silence_and_refusal_are_evidence_but_generation_failure_is_not(relationship_store):
    silent = await _turn("alice", "A message deliberately left unanswered.", status="silent")
    refusal = await _turn("alice", "A request explicitly declined.", status="declined")
    failed = await _turn("alice", "A provider failure.", status="failed", decision=False)
    notice = await _turn("alice", "A successfully sent failure notice.", decision=False)
    assert await _enqueue(silent)
    assert await _enqueue(refusal)
    assert not await _enqueue(failed)
    assert not await _enqueue(notice)
    batch = await evaluation_store.claim_evaluation("alice", "channel", batch_size=8, model="test")
    assert batch is not None
    assert [(row["outcome"], row["assistant"]) for row in batch.episodes] == [
        ("silent", ""),
        ("declined", "I understand."),
    ]
    await evaluation_store.release_evaluation(batch, "Synthetic shutdown", cancelled=True)


@pytest.mark.asyncio
async def test_nightly_maintenance_does_not_reset_established_relationships(relationship_store):
    await _turn("alice", "An established relationship.")
    async with relationship_store.factory() as db:
        await db.execute(
            update(UserRelation)
            .where(UserRelation.user_id == "alice")
            .values(
                affection=80.0,
                trust=75.0,
                dependence=65.0,
                resentment=12.0,
                familiarity=90.0,
            )
        )
        await db.commit()
    await persona_store.nightly_decay()
    async with relationship_store.factory() as db:
        row = await db.get(UserRelation, ("alice", "channel"))
    assert (row.affection, row.trust, row.dependence, row.resentment, row.familiarity) == (80.0, 75.0, 65.0, 12.0, 90.0)


@pytest.mark.asyncio
async def test_cancelled_embedding_keeps_relationship_and_memory_evidence_recoverable(
    relationship_store,
    monkeypatch: pytest.MonkeyPatch,
):
    config = LLMChatConfig(relationship_eval_debounce_seconds=0)
    entered = asyncio.Event()
    release = asyncio.Event()
    observed_batches = []

    async def embedding(_config, _text):
        entered.set()
        await release.wait()
        return [1.0, 0.0]

    async def evaluator(_config, _persona, _relationship, _facts, episodes, _channel):
        ids = tuple(episode["turn_id"] for episode in episodes)
        observed_batches.append(ids)
        return EvalResult(
            deltas={name: 4.0 if name == "trust" else 0.0 for name in AXIS_KEYS},
            impression="An attentive person.",
            relationship_description="A reliable shared interaction.",
            emotions=(),
            processed_turn_ids=ids,
            profile_patches=[],
            memory_items=[MemoryItem(text="We repaired a bicycle together.", importance=0.9)],
        )

    monkeypatch.setattr(memory_update, "embed_text", embedding)
    monkeypatch.setattr(chat_evaluation, "run_evaluation", evaluator)
    turn = await _turn("alice", "We repaired my bicycle together today.")
    warnings = []
    chat_evaluation.schedule_relationship_evaluation(
        config,
        turn_id=turn.id,
        user_id="alice",
        channel_id="channel",
        persona_prompt="A thoughtful conversational character.",
        warn=warnings.append,
    )
    await asyncio.wait_for(entered.wait(), timeout=10)
    await chat_evaluation.cancel_pending_evaluations()
    async with relationship_store.factory() as db:
        assert (await db.get(UserRelation, ("alice", "channel"))).trust == 30.0
        assert list((await db.execute(select(UserMemory.text))).scalars()) == []
        retained = await db.get(RelationshipEvidence, turn.id)
        assert retained.status == "pending"
        assert (await db.get(AgentEvent, retained.event_id)).status == "pending"
    release.set()
    await chat_evaluation.resume_relationship_evaluations(config, warnings.append)
    await _processed(relationship_store.factory, 1)
    async with relationship_store.factory() as db:
        assert (await db.get(UserRelation, ("alice", "channel"))).trust == 34.0
        assert list((await db.execute(select(UserMemory.text))).scalars()) == ["We repaired a bicycle together."]
    assert observed_batches == [(turn.id,), (turn.id,)]
    assert warnings == []


@pytest.mark.asyncio
async def test_memory_write_failure_rolls_back_the_entire_evaluation(
    relationship_store,
    monkeypatch: pytest.MonkeyPatch,
):
    turn = await _turn("alice", "We repaired my bicycle together today.")
    await _enqueue(turn)
    batch = await evaluation_store.claim_evaluation("alice", "channel", batch_size=8, model="test")
    result = _result(batch)
    result.memory_items.append(MemoryItem(text="We repaired a bicycle together.", importance=0.9))

    async def no_embedding(_config, _text):
        return None

    monkeypatch.setattr(memory_update, "embed_text", no_embedding)
    prepared = await memory_update.prepare_memory_updates(LLMChatConfig(), "alice", "channel", result)
    apply_memory = evaluation_store.apply_memory_updates

    async def interrupted_write(db, memory):
        await apply_memory(db, memory)
        await db.flush()
        raise OSError("Synthetic storage interruption")

    monkeypatch.setattr(evaluation_store, "apply_memory_updates", interrupted_write)
    with pytest.raises(OSError, match="Synthetic storage interruption"):
        await evaluation_store.commit_evaluation(batch, result, memory=prepared)
    async with relationship_store.factory() as db:
        assert (await db.get(UserRelation, ("alice", "channel"))).trust == 30.0
        assert (await db.get(UserAffect, ("alice", "channel"))).version == batch.version
        assert list((await db.execute(select(UserMemory.text))).scalars()) == []
        assert (await db.get(RelationshipEvidence, turn.id)).status == "running"
        assert (await db.get(AgentEvent, batch.event_ids[0])).status == "running"
    monkeypatch.setattr(evaluation_store, "apply_memory_updates", apply_memory)
    await evaluation_store.commit_evaluation(batch, result, memory=prepared)
    with pytest.raises(RelationshipConflict):
        await evaluation_store.commit_evaluation(batch, result, memory=prepared)
    async with relationship_store.factory() as db:
        assert (await db.get(UserRelation, ("alice", "channel"))).trust == 34.0
        assert list((await db.execute(select(UserMemory.text))).scalars()) == ["We repaired a bicycle together."]
        assert (await db.get(RelationshipEvidence, turn.id)).status == "processed"


@pytest.mark.asyncio
async def test_identity_merge_recovers_delayed_and_unqueued_turns_without_relabeling_speakers(relationship_store):
    await migration.initialize_relationship_store()
    claimed = await _turn("old", "Already queued under the old binding.")
    delayed = await _turn("old", "Finalized before the evidence task was scheduled.")
    recovered = await _turn("old", "Finalized before a process interruption.")
    await _enqueue(claimed)
    stale = await evaluation_store.claim_evaluation("old", "channel", batch_size=8, model="test")
    await identity.migrate_legacy_user_state("channel", ["old"], "current")
    await _enqueue(delayed)
    assert await recovery.recover_unqueued_evidence("test") == 1
    with pytest.raises(RelationshipConflict):
        await evaluation_store.commit_evaluation(stale, _result(stale))
    batch = await evaluation_store.claim_evaluation("old", "channel", batch_size=8, model="test")
    assert batch.user_id == "current"
    assert batch.turn_ids == (claimed.id, delayed.id, recovered.id)
    await evaluation_store.commit_evaluation(batch, _result(batch))
    async with relationship_store.factory() as db:
        assert await db.get(UserRelation, ("old", "channel")) is None
        assert (await db.get(UserRelation, ("current", "channel"))).trust == 34.0
        assert set((await db.execute(select(AgentTurn.user_id))).scalars()) == {"old"}
        rows = (await db.execute(select(RelationshipEvidence))).scalars().all()
        assert {(row.user_id, row.status) for row in rows} == {("current", "processed")}
    assert await recovery.recover_unqueued_evidence("test") == 0
