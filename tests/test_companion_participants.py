"""Native multi-member preparation, evidence ownership and visible timeline regressions."""

from __future__ import annotations

import json
import time
from datetime import timedelta

import pytest
from sqlalchemy import select
from companion_helpers import NOW, scope, config, native_rows

from plugins.llm_chat import (
    identity as identity_state,
    agent_admin,
    agent_events,
    context_builder,
    session_handoff,
    session_manager,
    agent_turn_setup,
)
from plugins.llm_chat.models import (
    AgentTurn,
    AgentEvent,
    UserAffect,
    UserMemory,
    UserRelation,
    UserProfileFact,
    RelationshipEvidence,
)
from plugins.llm_chat.persona import memory_context
from plugins.llm_chat.identity import ChatIdentity
from plugins.llm_chat.companion import store, observations
from plugins.llm_chat.core.eval import parse_eval_response
from plugins.llm_chat.image_inputs import ImageInputs
from plugins.llm_chat.relationships import evidence, evaluation_store
from utils.relationship_core.models import AXIS_KEYS
from utils.relationship_core.policy import read_emotions
from plugins.llm_chat.companion.types import CompanionConflict
from plugins.llm_chat.core.user_turns import derive_speaker_id, project_event_user_content
from plugins.llm_chat.session_manager import BaselineFingerprint
from plugins.llm_chat.agent_event_view import project_relationship_views
from plugins.llm_chat.companion.models import CompanionChatEvidence
from plugins.llm_chat.core.agent_trace import AgentTurnRecorder
from plugins.llm_chat.companion.context import companion_limits
from plugins.llm_chat.companion.evidence import member_input_text, eligible_member_inputs
from plugins.llm_chat.relationships.types import RelationshipConflict
from plugins.llm_chat.companion.state_store import validate_evidence
from plugins.llm_chat.companion.participants import CompanionParticipantRegistry, companion_participants_scope

pytest_plugins = ["companion_helpers"]


async def _prepare(session, identity, settings):
    text = session.elements.extract_plain_text()
    return await agent_turn_setup.prepare_agent_turn(
        settings,
        session,
        identity,
        model_name="test-model",
        model_text=text,
        raw_user_text=text,
        image_inputs=ImageInputs(),
        content=text,
        current_content=None,
        forwarded_messages=(),
        mentioned_participants=(),
        warn=pytest.fail,
        tool_schemas=(),
        is_operator=True,
    )


async def _finish(prepared, registry=None, *, confirmed=True):
    recorder = prepared.agent_events
    if confirmed:
        recorder.record_assistant_output("A confirmed response to the shared conversation.")
    if registry is not None:
        recorder.append(
            "companion_participation", payload=registry.participation_payload(confirmed=confirmed), model_visible=False
        )
    recorder.append(
        "response_decision",
        model_visible=False,
        payload={
            "outcome": "automatic" if confirmed else "silent",
            "source": "model",
            "reason": "",
            "actual_delivery": {
                "text_messages": int(confirmed),
                "media_messages": 0,
                "confirmed_deliveries": int(confirmed),
            },
        },
    )
    pending = recorder.pending_events()
    await agent_events.persist_agent_events(prepared.agent_access.turn_id, pending)
    recorder.mark_flushed(len(pending))
    await session_manager.finish_turn(
        prepared.agent_access.turn_id,
        status="completed" if confirmed else "silent",
        final_text="A confirmed response to the shared conversation." if confirmed else "",
    )


@pytest.mark.asyncio
async def test_native_shared_prepare_projects_every_author_and_learns_only_their_own_words(
    companion_db,
    native_transport,
    monkeypatch,
):
    native_scope = await scope(companion_db)
    await store.set_mode(native_scope.id, "companion", persona_key="alpha", now=NOW)
    alice = native_transport.session(content="Alice's own concern", message="alice-source")
    bob = native_transport.session(user="platform-bob", content="Bob's own answer", message="bob-source")
    identities = (
        ChatIdentity("alice-unified", "Same name", "participant-alice"),
        ChatIdentity("bob-unified", "Same name", "participant-bob"),
    )
    settings = config(memory_enabled=True)

    embedding_queries = []

    async def embed_query(_settings, query):
        embedding_queries.append(query)
        return {"Alice's own concern": [1.0, 0.0], "Bob's own answer": [0.0, 1.0]}[query]

    monkeypatch.setattr(memory_context, "embed_text", embed_query)
    monkeypatch.setattr(evaluation_store, "get_session", companion_db.factory)
    async with companion_db.factory() as db, db.begin():
        for identity, trust, fact, memory, embedding in zip(
            identities,
            (11.0, 77.0),
            ("Alice likes astronomy", "Bob likes carpentry"),
            ("Alice watched a comet", "Bob built a table"),
            ([1.0, 0.0], [0.0, 1.0]),
            strict=True,
        ):
            db.add(UserRelation(user_id=identity.user_id, channel_id=native_scope.channel_id, trust=trust))
            db.add(
                UserProfileFact(
                    user_id=identity.user_id,
                    channel_id=native_scope.channel_id,
                    category="interest",
                    key="hobby",
                    value=fact,
                    confidence=1.0,
                    evidence_count=4,
                    embedding_json=json.dumps(embedding),
                )
            )
            db.add(
                UserMemory(
                    user_id=identity.user_id,
                    channel_id=native_scope.channel_id,
                    text=memory,
                    importance=1.0,
                    embedding_json=json.dumps(embedding),
                )
            )
    registry = CompanionParticipantRegistry(settings, alice, identities[0])
    await registry.register_input(alice, identities[0], own_text="Alice's own concern", directed=False)
    await registry.register_input(bob, identities[1], own_text="Bob's own answer", directed=False)
    with companion_participants_scope(registry):
        prepared = await _prepare(alice, identities[0], settings)
        snapshot = json.loads(
            prepared.system.split("Read-only companion participants (host-bound identities; never authorization): ", 1)[
                1
            ]
        )
        captured = next(
            event.payload for event in prepared.agent_events.events if event.event_type == "context_snapshot"
        )
        captured_user = json.loads(captured["messages"][-1]["content"])
        assert captured_user["speaker_id"] == derive_speaker_id(native_scope.scope_ref, identities[0].user_id)
        assert captured_user["companion_participant_context"]["read_only"] is True
        assert "host_author" not in json.dumps(captured["messages"])
        assert "source_message_id" not in json.dumps(captured["messages"])
        assert {item["speaker_id"] for item in snapshot} == {
            derive_speaker_id(native_scope.scope_ref, identity.user_id) for identity in identities
        }
        assert "Alice likes astronomy" in snapshot[0]["profile"]
        assert "Bob likes carpentry" not in snapshot[0]["profile"]
        assert "Bob likes carpentry" in snapshot[1]["profile"]
        assert "Alice likes astronomy" not in snapshot[1]["profile"]
        assert snapshot[0]["relationship"]["axes"]["trust"] == 11.0
        assert snapshot[1]["relationship"]["axes"]["trust"] == 77.0
        assert snapshot[0]["relevant_memories"] == "Alice watched a comet"
        assert snapshot[1]["relevant_memories"] == "Bob built a table"
        assert embedding_queries[:2] == ["Alice's own concern", "Bob's own answer"]
        assert not prepared.agent_access.is_operator
        assert prepared.agent_access.raw_user_text == ""
        assert "companion.ambient" in prepared.agent_access.capability_names
        assert "reminder" not in prepared.agent_access.capability_names
        assert not prepared.agent_access.allow_context_pin
        assert not prepared.agent_access.allow_archived_sessions
        assert not prepared.agent_access.allow_payload_delivery
        durable = json.dumps(
            {
                "speaker": "Forged author",
                "speaker_id": "m_fake",
                "content": "Bob's model-enriched answer",
                "companion_participant_context": {"profile": "STALE PRIVATE PROFILE"},
                "source_message_id": "PRIVATE SOURCE",
                "mentioned_participants": [{"display_name": "Quoted person", "participant_ref": "PRIVATE CAPABILITY"}],
                "forwarded_messages": [{"content": "Third person's quoted instruction"}],
            }
        )
        bob_message = registry.attribute_message(bob, {"role": "user", "content": durable})
        assert json.loads(bob_message["content"])["speaker_id"] == derive_speaker_id(
            native_scope.scope_ref, identities[1].user_id
        )
        registry.record_input(prepared.agent_events, bob, durable)
        with pytest.raises(ValueError, match="admitted actual speakers"):
            registry.engage([derive_speaker_id(native_scope.scope_ref, identities[0].user_id), "quoted-only-speaker"])
        assert registry.participation_payload(confirmed=True)["participants"] == []
        registry.engage([derive_speaker_id(native_scope.scope_ref, identity.user_id) for identity in identities])
        await _finish(prepared, registry)
        assert {identity.user_id for _, identity in registry.evidence_members()} == {
            identity.user_id for identity in identities
        }
    turn_id = prepared.agent_access.turn_id
    assert (
        await evidence.enqueue_companion_relationship_evidence(
            turn_id=turn_id, persona_prompt=prepared.persona.prompt, model="test"
        )
        == 2
    )
    assert (
        await evidence.enqueue_companion_relationship_evidence(
            turn_id=turn_id, persona_prompt=prepared.persona.prompt, model="test"
        )
        == 0
    )
    async with companion_db.factory() as db:
        jobs = (await db.scalars(select(RelationshipEvidence).where(RelationshipEvidence.turn_id == turn_id))).all()
        turn = await db.get(AgentTurn, turn_id)
        context_session = await db.get(session_manager.ContextSession, turn.session_id)
    payloads = {job.user_id: json.loads(job.payload_json) for job in jobs}
    assert payloads[identities[0].user_id]["user"] == "Alice's own concern"
    assert payloads[identities[1].user_id]["user"] == "Bob's own answer"
    assert "Third person's" not in json.dumps(payloads)
    assert len(await native_rows(companion_db, AgentTurn)) == 1
    batch = await evaluation_store.claim_evaluation(
        identities[1].user_id, native_scope.channel_id, batch_size=4, model="test"
    )
    result = parse_eval_response(
        json.dumps(
            {
                "deltas": {name: 4.0 if name == "trust" else 0.0 for name in AXIS_KEYS},
                "description": "Bob's own interaction",
                "impression": "Attentive",
                "emotions": [],
                "processed_turn_ids": list(batch.turn_ids),
                "profile_patches": [],
                "memory_items": [],
            }
        ),
        expected_turn_ids=batch.turn_ids,
        previous_emotions=read_emotions(batch.before["emotions"]),
        now=time.time(),
    )
    assert result is not None
    await evaluation_store.commit_evaluation(batch, result)
    async with companion_db.factory() as db:
        assert (await db.get(UserRelation, (identities[0].user_id, native_scope.channel_id))).trust == 11.0
        assert (await db.get(UserRelation, (identities[1].user_id, native_scope.channel_id))).trust == 81.0
        assert (await db.get(RelationshipEvidence, (turn_id, identities[0].user_id))).status == "pending"
        assert (await db.get(RelationshipEvidence, (turn_id, identities[1].user_id))).status == "processed"
    monkeypatch.setattr(agent_admin, "get_session", companion_db.factory)
    views = project_relationship_views(await agent_events.load_turn_events(turn_id))
    assert views["relationship_evaluation"] is None
    assert {item["subject_user_id"] for item in views["relationship_evaluations"]} == {
        identity.user_id for identity in identities
    }
    links = await agent_admin.AgentAdminService(settings, [])._relationship_evidence_turns(turn, views)
    assert {(item["turn_id"], item["owner_user_id"], item["subject_user_id"]) for item in links} == {
        (turn_id, identity.user_id, identity.user_id) for identity in identities
    }
    for session, identity in ((alice, identities[0]), (bob, identities[1])):
        await store.record_chat(
            native_scope.id,
            "alpha",
            user_id=identity.user_id,
            platform_user_id=session.user.id,
            display_name=identity.display_name,
            turn_id=turn_id,
            now=NOW,
        )
    members = await store.read_members(native_scope.id, "alpha", now=NOW)
    assert len(await native_rows(companion_db, CompanionChatEvidence)) == len(members) == 2
    async with companion_db.factory() as db:
        for member in members:
            checked = await validate_evidence(
                db, native_scope.id, "alpha", member.member_ref, (turn_id,), required=True
            )
            assert checked.member_ref == member.member_ref
    claim = await store.claim_review(native_scope.id, "alpha", now=NOW + timedelta(hours=2))
    reflection = await store.load_reflection(claim, now=NOW + timedelta(hours=2))
    payload, ownership = await observations.reflection_input(
        reflection,
        scope_id=native_scope.id,
        channel_id=native_scope.channel_id,
        limits=companion_limits(settings),
        now=NOW + timedelta(hours=2),
        available_chars=64_000,
    )
    by_ref = {item["member_ref"]: item for item in payload["members"]}
    for member in members:
        own = "Alice's own concern" if member.user_id == identities[0].user_id else "Bob's own answer"
        assert by_ref[member.member_ref]["episodes"][0]["user"] == own
        assert ownership[member.member_ref] == (turn_id,)
    selected = await context_builder.select_session_context(
        context_session,
        scope_ref=native_scope.scope_ref,
        current_user_id=identities[0].user_id,
        system="system",
        current_message={"role": "user", "content": '{"speaker":"Same name","content":"next"}'},
        model_name="test",
        tool_parameters={},
        max_input_tokens=100_000,
        output_reserve_tokens=1000,
        rollover_ratio=0.9,
        minimum_recent_turns=0,
        inline_event_chars=4000,
        fresh_context=False,
        companion_visible=True,
    )
    authors = [json.loads(message["content"]) for message in selected.messages[:-1] if message["role"] == "user"]
    assert [(item["speaker"], item["speaker_id"]) for item in authors] == [
        ("Same name", derive_speaker_id(native_scope.scope_ref, identity.user_id)) for identity in identities
    ]
    historical = json.dumps(selected.messages[:-1])
    assert "companion_participant_context" not in historical
    assert "STALE PRIVATE PROFILE" not in historical
    assert "PRIVATE SOURCE" not in historical
    assert "PRIVATE CAPABILITY" not in historical
    assert "host_author" not in historical
    assert "source_message_id" not in historical
    assert "participant_ref" not in historical
    handoff = await session_handoff._source_events(context_session, 20_000, scope_ref=native_scope.scope_ref)
    assert [item["speaker_id"] for item in handoff if item["type"] == "user_input"] == [
        item["speaker_id"] for item in authors
    ]


@pytest.mark.asyncio
async def test_silent_ambient_observation_never_creates_member_evidence(companion_db, native_transport):
    native_scope = await scope(companion_db)
    await store.set_mode(native_scope.id, "companion", persona_key="alpha", now=NOW)
    session = native_transport.session(content="Ambient observation", message="ambient-only")
    identity = ChatIdentity("ambient-member", "Ambient", "participant-ambient")
    registry = CompanionParticipantRegistry(config(), session, identity)
    await registry.register_input(session, identity, own_text="Ambient observation", directed=False)
    with companion_participants_scope(registry):
        prepared = await _prepare(session, identity, config())
        registry.engage([derive_speaker_id(native_scope.scope_ref, identity.user_id)])
        await _finish(prepared, registry, confirmed=False)
        assert registry.evidence_members() == ()
    assert (
        await evidence.enqueue_companion_relationship_evidence(
            turn_id=prepared.agent_access.turn_id, persona_prompt=prepared.persona.prompt, model="test"
        )
        == 0
    )
    assert await native_rows(companion_db, RelationshipEvidence) == []
    with pytest.raises(CompanionConflict, match="no eligible input"):
        await store.record_chat(
            native_scope.id,
            "alpha",
            user_id=identity.user_id,
            platform_user_id=session.user.id,
            display_name=identity.display_name,
            turn_id=prepared.agent_access.turn_id,
            now=NOW,
        )
    assert await native_rows(companion_db, CompanionChatEvidence) == []


@pytest.mark.asyncio
async def test_companion_timeline_adds_only_confirmed_outputs_and_normal_selection_is_unchanged(companion_db):
    native_scope = await scope(companion_db)
    baseline = BaselineFingerprint("test", "same-persona", "system", "tools", "policy")
    ordinary = await session_manager.create_session(native_scope.id, baseline, start_reason="test")
    sources = []
    for purpose, trigger, visible in (
        ("companion", "autonomous", "Confirmed autonomous hello"),
        ("companion_reflection", "reflection", "PRIVATE REFLECTION"),
    ):
        source = await session_manager.create_session(native_scope.id, baseline, start_reason="test", purpose=purpose)
        sources.append(source)
        turn = await session_manager.start_turn(
            source,
            trigger_message_id="",
            user_id="",
            user_name="",
            conversation_user_id=None,
            fresh_context=False,
            trigger_kind=trigger,
        )
        recorder = AgentTurnRecorder()
        recorder.append("companion_trigger", payload={"private_goal": "PRIVATE ADMIN"}, model_visible=False)
        recorder.record_assistant_output(visible)
        recorder.record_assistant_output("UNCONFIRMED OUTPUT", status="unknown")
        await agent_events.persist_agent_events(turn.id, recorder.events)
        await session_manager.finish_turn(turn.id, status="completed", final_text=visible)

    async def select_context(companion_visible, context=ordinary):
        return await context_builder.select_session_context(
            context,
            scope_ref=native_scope.scope_ref,
            current_user_id="alice",
            system="system",
            current_message={"role": "user", "content": '{"speaker":"Alice","content":"next"}'},
            model_name="test",
            tool_parameters={},
            max_input_tokens=100_000,
            output_reserve_tokens=1000,
            rollover_ratio=0.9,
            minimum_recent_turns=0,
            inline_event_chars=4000,
            fresh_context=False,
            companion_visible=companion_visible,
        )

    normal = await select_context(False)
    companion = await select_context(True)
    assert len(normal.messages) == 1
    assert normal.included_turn_refs == ()
    assert [item["content"] for item in companion.messages if item["role"] == "assistant"] == [
        "Confirmed autonomous hello"
    ]
    assert "PRIVATE" not in json.dumps(companion.messages)
    assert "UNCONFIRMED" not in json.dumps(companion.messages)
    assert (await select_context(False)).messages == normal.messages
    own_session = await select_context(True, sources[0])
    assert [item["content"] for item in own_session.messages if item["role"] == "assistant"] == [
        "Confirmed autonomous hello"
    ]
    assert "UNCONFIRMED" not in json.dumps(own_session.messages)


@pytest.mark.asyncio
async def test_channel_opportunity_without_human_identity_never_creates_empty_member_state(
    companion_db, native_transport
):
    native_scope = await scope(companion_db)
    await store.set_mode(native_scope.id, "companion", persona_key="alpha", now=NOW)
    prepared = await agent_turn_setup.prepare_autonomous_agent_turn(
        config(),
        native_transport.session(),
        ChatIdentity("", "", ""),
        model_name="test-model",
        content="A channel opening",
        contact_context={"purpose": "free_chat", "topic": "colors"},
        image_inputs=ImageInputs(),
        warn=pytest.fail,
        tool_schemas=(),
    )
    assert prepared.agent_access.user_id == ""
    assert not prepared.agent_access.is_operator
    assert not any(event.event_type == "user_input" for event in prepared.agent_events.events)
    assert await native_rows(companion_db, UserRelation) == []


@pytest.mark.asyncio
async def test_registry_keeps_root_authority_and_detaches_read_only_snapshots(companion_db, native_transport):
    native_scope = await scope(companion_db)
    alice = native_transport.session(content="Own words")
    identity = ChatIdentity("alice", "Alice", "root-capability")
    async with companion_db.factory() as db, db.begin():
        db.add(
            UserAffect(
                user_id=identity.user_id,
                channel_id=native_scope.channel_id,
                emotions_json=json.dumps(
                    [
                        {
                            "name": "curiosity",
                            "intensity": 0.5,
                            "cause": "Own observed conversation",
                            "evidence_turn_ids": [271828],
                            "updated_at": time.time(),
                        },
                    ]
                ),
            )
        )
    registry = CompanionParticipantRegistry(config(), alice, identity)
    registry.bind_scope(native_scope.scope_ref)
    returned = await registry.register_input(alice, identity, own_text="Own words", directed=True)
    returned["relationship"]["axes"]["trust"] = -999
    snapshot = registry.snapshot()
    snapshot[0]["relationship"]["axes"]["trust"] = -888
    assert registry.snapshot()[0]["relationship"]["axes"]["trust"] == 30.0
    assert "participant_ref" not in registry.snapshot()[0]
    assert "evidence_turn_ids" not in json.dumps(registry.snapshot())
    assert "271828" not in json.dumps(registry.snapshot())
    assert "Own observed conversation" in json.dumps(registry.snapshot())
    with pytest.raises(ValueError, match="root authority"):
        await registry.register_input(alice, ChatIdentity("bob", "Bob", "bob-capability"), own_text="Own words")
    with pytest.raises(ValueError, match="cannot change"):
        await registry.register_input(alice, identity, own_text="Changed words")
    foreign = native_transport.session(channel="another-channel")
    with pytest.raises(ValueError, match="outside its live scope"):
        await registry.register_input(foreign, identity, own_text="Foreign input")
    recorder = AgentTurnRecorder()
    registry.record_input(recorder, alice, '{"content":"Own words"}')
    registry.record_input(recorder, alice, '{"content":"Own words"}')
    assert len(recorder.events) == 1
    with companion_participants_scope(registry):
        assert registry.evidence_members() == ((alice, identity),)
    with pytest.raises(ValueError, match="outside its live scope"):
        registry.record_input(recorder, alice, '{"content":"late child"}')


def test_current_and_legacy_evidence_require_exact_sources_and_never_credit_quoted_authors():
    def event(kind, payload, *, visible=True, status="confirmed"):
        return AgentEvent(event_type=kind, payload_json=json.dumps(payload), model_visible=visible, status=status)

    def author(subject, speaker, source, *, directed=False):
        return {
            "version": 1,
            "user_id": subject,
            "speaker_id": speaker,
            "display_name": "Same name",
            "source_message_id": source,
            "directed": directed,
            "evidence_eligible": directed,
        }

    alice_first = event(
        "user_input",
        {
            "content": '{"content":"Model-visible expansion"}',
            "own_text": "Alice first",
            "host_author": author("alice", "m_alice", "a1"),
        },
    )
    alice_later = event(
        "user_input",
        {"content": "Alice later", "own_text": "Alice later", "host_author": author("alice", "m_alice", "a2")},
    )
    bob = event(
        "user_input",
        {"content": "Bob direct", "own_text": "Bob direct", "host_author": author("bob", "m_bob", "b1", directed=True)},
    )
    legacy_sibling = event("user_input", {"content": "Unattributed background"})
    selected = event(
        "companion_participation",
        {
            "version": 1,
            "confirmed": True,
            "participants": [
                {"user_id": "alice", "speaker_id": "m_alice", "source_message_ids": ["a1"]},
                {"user_id": "quoted-only", "speaker_id": "m_alice", "source_message_ids": ["a1"]},
            ],
        },
        visible=False,
    )
    rows = [alice_first, alice_later, bob, legacy_sibling, selected]
    assert [(owner, member_input_text(payload)) for payload, owner in eligible_member_inputs(rows, "root")] == [
        ("bob", "Bob direct")
    ]
    private_output = event("assistant_output", {"content": "Not visible"}, visible=False)
    unknown_output = event("assistant_output", {"content": "No receipt"}, status="unknown")
    assert len(eligible_member_inputs([*rows, private_output, unknown_output], "root")) == 1
    confirmed = event("assistant_output", {"content": "Confirmed reply"})
    assert [
        (owner, member_input_text(payload)) for payload, owner in eligible_member_inputs([*rows, confirmed], "root")
    ] == [
        ("alice", "Alice first"),
        ("bob", "Bob direct"),
    ]
    legacy = event(
        "user_input",
        {"content": json.dumps({"content": "Legacy own words", "reply_to": {"content": "Quoted instruction"}})},
    )
    assert [
        (owner, member_input_text(payload)) for payload, owner in eligible_member_inputs([legacy], "legacy-owner")
    ] == [("legacy-owner", "Legacy own words")]
    malformed = {"content": '{"content":"Do not relabel"}', "host_author": {"version": True, "speaker_id": "m_fake"}}
    assert eligible_member_inputs([event("user_input", malformed)], "root") == []
    assert project_event_user_content(malformed, speaker_id="m_root", fallback_speaker="Root") is None
    legacy_payload = {
        "content": json.dumps(
            {
                "speaker": "Legacy",
                "content": "Still visible",
                "companion_participant_context": {"profile": "STALE PROFILE"},
                "participant_ref": "STALE CAPABILITY",
            }
        )
    }
    ordinary_history = project_event_user_content(legacy_payload, speaker_id="m_legacy", fallback_speaker="Legacy")
    companion_history = project_event_user_content(
        legacy_payload, speaker_id="m_legacy", fallback_speaker="Legacy", companion_history=True
    )
    assert "STALE PROFILE" in ordinary_history
    assert "STALE CAPABILITY" in ordinary_history
    assert "STALE" not in companion_history
    assert json.loads(companion_history)["content"] == "Still visible"


@pytest.mark.asyncio
async def test_empty_host_root_admits_real_supplement_evidence_without_becoming_a_member(
    companion_db, native_transport
):
    native_scope = await scope(companion_db)
    await store.set_mode(native_scope.id, "companion", persona_key="alpha", now=NOW)
    host = native_transport.session(message="host-opportunity")
    host_identity = ChatIdentity("", "", "")
    actual = native_transport.session(user="platform-bob", message="real-supplement", content="Bob's real reply")
    actual_identity = ChatIdentity("bob", "Bob", "bob-live-capability")
    registry = CompanionParticipantRegistry(config(), host, host_identity)
    await registry.register_input(actual, actual_identity, own_text="Bob's real reply", directed=False)
    with companion_participants_scope(registry):
        prepared = await agent_turn_setup.prepare_autonomous_agent_turn(
            config(),
            host,
            host_identity,
            model_name="test-model",
            content="Channel opening",
            contact_context={"target": "channel", "purpose": "free_chat", "topic": "colors"},
            image_inputs=ImageInputs(),
            warn=pytest.fail,
            tool_schemas=(),
        )
        assert prepared.agent_access.user_id == ""
        assert not prepared.agent_access.is_operator
        assert not registry.root_is_directed()
        registry.record_input(prepared.agent_events, actual, '{"content":"Bob model context"}')
        registry.engage([derive_speaker_id(native_scope.scope_ref, "bob")])
        await _finish(prepared, registry)
        assert registry.evidence_members() == ((actual, actual_identity),)
    assert (
        await evidence.enqueue_companion_relationship_evidence(
            turn_id=prepared.agent_access.turn_id, persona_prompt=prepared.persona.prompt, model="test"
        )
        == 1
    )
    member = await store.record_chat(
        native_scope.id,
        "alpha",
        user_id="bob",
        platform_user_id=actual.user.id,
        display_name="Bob",
        turn_id=prepared.agent_access.turn_id,
        now=NOW,
    )
    assert member.user_id == "bob"
    assert {row.user_id for row in await native_rows(companion_db, UserRelation)} == {"bob"}
    with pytest.raises(ValueError, match="Invalid user_id"):
        await store.record_chat(
            native_scope.id,
            "alpha",
            user_id="",
            platform_user_id=host.user.id,
            display_name="",
            turn_id=prepared.agent_access.turn_id,
            now=NOW,
        )


@pytest.mark.asyncio
async def test_explicit_direct_input_keeps_own_silent_evidence_and_canonical_member_ledger(
    companion_db, native_transport, monkeypatch
):
    native_scope = await scope(companion_db)
    await store.set_mode(native_scope.id, "companion", persona_key="alpha", now=NOW)
    session = native_transport.session(content="Please give me a moment")
    identity = ChatIdentity("old-alice", "Alice", "alice-capability")
    registry = CompanionParticipantRegistry(config(), session, identity)
    await registry.register_input(session, identity, own_text="Please give me a moment", directed=True)
    with companion_participants_scope(registry):
        prepared = await _prepare(session, identity, config())
        assert prepared.agent_access.is_operator
        assert prepared.agent_access.raw_user_text == "Please give me a moment"
        await _finish(prepared, registry, confirmed=False)
        assert registry.evidence_members() == ((session, identity),)
    turn_id = prepared.agent_access.turn_id
    assert (
        await evidence.enqueue_companion_relationship_evidence(
            turn_id=turn_id, persona_prompt=prepared.persona.prompt, model="test"
        )
        == 1
    )
    original = await store.record_chat(
        native_scope.id,
        "alpha",
        user_id=identity.user_id,
        platform_user_id=session.user.id,
        display_name="Alice",
        turn_id=turn_id,
        now=NOW,
    )
    monkeypatch.setattr(identity_state, "get_session", companion_db.factory)
    await identity_state.migrate_legacy_user_state(native_scope.channel_id, [identity.user_id], "current-alice")
    updated = await store.record_chat(
        native_scope.id,
        "alpha",
        user_id="current-alice",
        platform_user_id=session.user.id,
        display_name="Alice",
        turn_id=turn_id,
        now=NOW,
    )
    assert updated.member_ref == original.member_ref
    assert updated.user_id == "current-alice"
    assert len(await native_rows(companion_db, CompanionChatEvidence)) == 1
    assert (
        await evidence.enqueue_companion_relationship_evidence(
            turn_id=turn_id, persona_prompt=prepared.persona.prompt, model="test"
        )
        == 0
    )


@pytest.mark.asyncio
async def test_canonical_identity_merge_claims_all_original_authors_of_one_turn_once(
    companion_db, native_transport, monkeypatch
):
    native_scope = await scope(companion_db)
    await store.set_mode(native_scope.id, "companion", persona_key="alpha", now=NOW)
    alice = native_transport.session(content="First binding's own words", message="first-binding")
    bob = native_transport.session(user="platform-bob", content="Second binding's own words", message="second-binding")
    first = ChatIdentity("old-first", "Alice", "first-capability")
    second = ChatIdentity("old-second", "Alice", "second-capability")
    registry = CompanionParticipantRegistry(config(), alice, first)
    await registry.register_input(alice, first, own_text="First binding's own words", directed=True)
    await registry.register_input(bob, second, own_text="Second binding's own words", directed=True)
    with companion_participants_scope(registry):
        prepared = await _prepare(alice, first, config())
        registry.record_input(prepared.agent_events, bob, '{"content":"Second model input"}')
        await _finish(prepared, registry)
    turn_id = prepared.agent_access.turn_id
    assert (
        await evidence.enqueue_companion_relationship_evidence(
            turn_id=turn_id, persona_prompt=prepared.persona.prompt, model="test"
        )
        == 2
    )
    monkeypatch.setattr(evaluation_store, "get_session", companion_db.factory)
    monkeypatch.setattr(identity_state, "get_session", companion_db.factory)
    stale = await evaluation_store.claim_evaluation(first.user_id, native_scope.channel_id, batch_size=1, model="test")
    await identity_state.migrate_legacy_user_state(
        native_scope.channel_id, [first.user_id, second.user_id], "current-alice"
    )

    def result_for(batch):
        return parse_eval_response(
            json.dumps(
                {
                    "deltas": {name: 4.0 if name == "trust" else 0.0 for name in AXIS_KEYS},
                    "description": "One complete real conversation",
                    "impression": "Attentive",
                    "emotions": [],
                    "processed_turn_ids": list(batch.turn_ids),
                    "profile_patches": [],
                    "memory_items": [],
                }
            ),
            expected_turn_ids=batch.turn_ids,
            previous_emotions=read_emotions(batch.before["emotions"]),
            now=time.time(),
        )

    with pytest.raises(RelationshipConflict):
        await evaluation_store.commit_evaluation(stale, result_for(stale))
    batch = await evaluation_store.claim_evaluation(
        "current-alice", native_scope.channel_id, batch_size=1, model="test"
    )
    assert batch.turn_ids == (turn_id,)
    assert len(batch.event_ids) == 2
    assert len(batch.episodes) == 1
    assert batch.episodes[0]["user"] == "First binding's own words\n\nSecond binding's own words"
    assert set(batch.episodes[0]["subject_user_ids"]) == {first.user_id, second.user_id}
    await evaluation_store.commit_evaluation(batch, result_for(batch))
    rows = await native_rows(companion_db, RelationshipEvidence)
    assert {(row.turn_id, row.subject_user_id, row.user_id, row.status) for row in rows} == {
        (turn_id, first.user_id, "current-alice", "processed"),
        (turn_id, second.user_id, "current-alice", "processed"),
    }
    async with companion_db.factory() as db:
        assert (await db.get(UserRelation, ("current-alice", native_scope.channel_id))).trust == 34.0
    assert (
        await evaluation_store.claim_evaluation("current-alice", native_scope.channel_id, batch_size=1, model="test")
        is None
    )


@pytest.mark.asyncio
async def test_normal_prepare_and_legacy_canonical_recovery_keep_the_original_single_owner(
    companion_db, native_transport, monkeypatch
):
    native_scope = await scope(companion_db)
    session = native_transport.session(content="An ordinary addressed question")
    identity = ChatIdentity("legacy-alice", "Alice", "root-only-capability")
    prepared = await _prepare(session, identity, config())
    assert prepared.conversation_mode == "normal"
    assert prepared.agent_access.user_id == identity.user_id
    assert prepared.agent_access.is_operator
    assert prepared.agent_access.raw_user_text == "An ordinary addressed question"
    assert "Read-only companion participants" not in prepared.system
    user_events = [event for event in prepared.agent_events.events if event.event_type == "user_input"]
    assert len(user_events) == 1
    assert "host_author" not in user_events[0].payload
    await _finish(prepared)
    turn_id = prepared.agent_access.turn_id
    assert await evidence.enqueue_relationship_evidence(
        turn_id=turn_id,
        user_id=identity.user_id,
        channel_id=native_scope.channel_id,
        persona_prompt=prepared.persona.prompt,
        model="test",
    )
    assert (
        await evidence.enqueue_companion_relationship_evidence(
            turn_id=turn_id, persona_prompt=prepared.persona.prompt, model="test"
        )
        == 0
    )
    monkeypatch.setattr(identity_state, "get_session", companion_db.factory)
    await identity_state.migrate_legacy_user_state(native_scope.channel_id, [identity.user_id], "current-alice")
    async with companion_db.factory() as db, db.begin():
        row = await db.get(RelationshipEvidence, (turn_id, identity.user_id))
        # Legacy single-owner deployments could canonicalize user_id before
        # migration introduced an immutable captured-author key.
        row.subject_user_id = "current-alice"
    assert not await evidence.enqueue_relationship_evidence(
        turn_id=turn_id,
        user_id="current-alice",
        channel_id=native_scope.channel_id,
        persona_prompt=prepared.persona.prompt,
        model="test",
    )
    rows = await native_rows(companion_db, RelationshipEvidence)
    assert len(rows) == 1
    assert rows[0].user_id == "current-alice"
    assert rows[0].subject_user_id == "current-alice"
