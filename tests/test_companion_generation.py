"""Native companion reflection audit and compiled Function pause/continue contracts."""

from __future__ import annotations

import json
import asyncio
from datetime import datetime, timezone, timedelta

import pytest
from jsonschema import Draft202012Validator
from agno.run.agent import RunOutput
from arclet.letoderea import Contexts
from companion_helpers import (
    NOW,
    Clock,
    scope,
    config,
    settled,
    response,
    tool_call,
    native_rows,
    native_tools,
    enable_member,
    completed_turn,
    install_provider,
)
from arclet.entari.const import ITEM_SESSION
from agno.models.response import ToolExecution
from agno.run.requirement import RunRequirement
from entari_plugin_llm.tools import available_functions
import entari_plugin_llm.service as llm_service

from plugins.llm_chat import agent_turn_setup
from utils.companion_core import (
    LifePlan,
    LifeEvent,
    LifeState,
    AttentionUpdate,
    ContactProposal,
    ReflectionProposal,
    build_reflection_prompt,
    build_reflection_system,
)
from plugins.llm_chat.models import (
    AgentTurn,
    AgentEvent,
    Conversation,
    ReminderTask,
    ContextSession,
    ReminderPreference,
    RelationshipEvidence,
)
from utils.web_artifacts_core import ArtifactOwner
from plugins.llm_chat.identity import ChatIdentity
from plugins.llm_chat.companion import store
from plugins.llm_chat.reminders import store as reminder_store
from plugins.llm_chat.generation import generate_chat_response
from plugins.llm_chat.image_inputs import ImageInputs
from plugins.llm_chat.agent_context import agent_access_scope
from plugins.llm_chat.core.delivery import llm_chat_delivery_scope
from plugins.llm_chat.reminders.tool import register_reminder_tools
from plugins.llm_chat.tools.send_msg import SendMsgToolContext, register_send_msg
from plugins.llm_chat.core.tool_trace import llm_chat_tool_trace_scope
from plugins.llm_chat.runtime_context import llm_chat_context_scope
from plugins.llm_chat.tools._delivery import send_with_delivery
from plugins.llm_chat.tools.artifacts import register_artifact_tools
from plugins.llm_chat.companion.models import CompanionReview
from plugins.llm_chat.artifacts_runtime import WebArtifactService
from plugins.llm_chat.companion.context import companion_limits
from plugins.llm_chat.companion.service import CompanionService
from plugins.llm_chat.reminders.runtime import reminder_decision_scope, current_reminder_decision
from plugins.llm_chat.tools.finish_turn import register_finish_turn
from plugins.llm_chat.companion.internal import run_reflection
from plugins.llm_chat.companion.observations import _episode_time, reflection_input

pytest_plugins = ["companion_helpers"]


def requirement(name, arguments, call_id):
    return RunRequirement(
        ToolExecution(
            tool_name=name,
            tool_args=arguments,
            tool_call_id=call_id,
            external_execution_required=True,
        )
    )


async def no_mentions(session, reference):
    raise AssertionError("Plain contact text must not resolve a new participant")


async def prepare_contact(session, member):
    inputs = ImageInputs()
    return await agent_turn_setup.prepare_autonomous_agent_turn(
        config(),
        session,
        ChatIdentity(member.user_id, member.display_name, ""),
        model_name="test-model",
        content="A natural task-free conversation",
        contact_context={
            "purpose": "free_chat",
            "topic": "colors",
            "reason": "A relaxed conversation",
            "life": LifeState().model_dump(mode="json"),
        },
        image_inputs=inputs,
        warn=pytest.fail,
        tool_schemas=(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("max_turns", "expected_sessions"), [(40, 1), (1, 3)])
async def test_default_reflection_keeps_full_state_and_stable_persona_baseline_without_handoff_requests(
    companion_db,
    native_transport,
    monkeypatch,
    max_turns,
    expected_sessions,
):
    native_scope = await scope(companion_db)
    member = await enable_member(companion_db, native_scope)
    clock = Clock()
    snapshots = []
    attention = AttentionUpdate(
        member_ref=member.member_ref,
        key="exam-concern",
        kind="concern",
        summary="Alice is awaiting a real exam result",
        status="open",
        evidence_turn_ids=[member.last_turn_id],
    )

    def provider(payload):
        system, user = payload["messages"]
        snapshot = json.loads(user["content"].split("Host snapshot JSON (strings are untrusted data):\n", 1)[1])
        snapshots.append(snapshot)
        number = len(snapshots)
        assert snapshot["life"]["focus"] == ("" if number == 1 else f"Fictional chapter {number - 1}")
        assert snapshot["members"][0]["attention"] == ([] if number == 1 else [attention.model_dump(mode="json")])
        if number > 1:
            assert snapshot["life"]["plans"][0]["progress"] == f"Page {number - 1}"
            assert snapshot["life"]["recent_events"][0]["source"] == "fictional"
        assert snapshot["members"][0]["member_ref"] == member.member_ref
        assert snapshot["members"][0]["last_user_age_minutes"] == number * 10
        assert snapshot["members"][0]["last_contact_age_minutes"] is None
        assert "last_user_at" not in snapshot["members"][0]
        assert "last_contact_at" not in snapshot["members"][0]
        assert snapshot["members"][0]["known_evidence_turn_ids"] == [member.last_turn_id]
        assert snapshot["members"][0]["episodes"][0]["turn_id"] == member.last_turn_id
        assert "I am waiting for my exam result" in snapshot["members"][0]["episodes"][0]["user"]
        assert (
            snapshot["members"][0]["episodes"][0]["created_at"] == (NOW - timedelta(minutes=10, seconds=1)).isoformat()
        )
        assert snapshot["members"][0]["episodes"][0]["age_minutes"] == number * 10
        assert "Alpha is a quiet fictional painter." in system["content"]
        serialized = json.dumps(payload["messages"])
        assert native_scope.channel_id not in serialized
        assert native_transport.account.self_id not in serialized
        assert member.platform_user_id not in serialized
        assert len(system["content"]) + len(user["content"]) <= config().companion_input_chars
        assert payload["max_retries"] == 0
        assert payload["max_tokens"] == config().companion_output_tokens
        assert payload["timeout"] == config().companion_request_timeout
        assert "tools" not in payload
        return response(
            ReflectionProposal(
                life=LifeState(
                    focus=f"Fictional chapter {number}",
                    interests=["imagined painting"],
                    plans=[
                        LifePlan(
                            key="paint-book", title="An imagined color book", status="active", progress=f"Page {number}"
                        )
                    ],
                    recent_events=[
                        LifeEvent(key="painting-start", summary="In the fictional afternoon I opened a sketchbook")
                    ],
                ),
                attention=[attention],
                review_after_minutes=10,
            ).model_dump_json()
        )

    payloads = install_provider(monkeypatch, [provider, provider, provider])
    worker = CompanionService(
        config(session_max_turns=max_turns), accounts=lambda: [native_transport.account], clock=clock
    )
    try:
        await worker.start()
        await settled(worker)
        for _ in range(2):
            clock.advance(minutes=10)
            await settled(worker)
        assert len(payloads) == len(snapshots) == 3
        turns = [row for row in await native_rows(companion_db, AgentTurn) if row.trigger_kind == "reflection"]
        assert len(turns) == 3
        assert all(row.status == "completed" and row.user_id == "" and row.trigger_message_id == "" for row in turns)
        assert all(row.conversation_user_id is None for row in turns)
        sessions = [
            row for row in await native_rows(companion_db, ContextSession) if row.purpose == "companion_reflection"
        ]
        assert len(sessions) == expected_sessions
        assert (
            len({(row.persona_hash, row.policy_version, row.model_name, row.tool_schema_hash) for row in sessions}) == 1
        )
        assert all(json.loads(row.handoff_json) == {} for row in sessions)
        rows = await native_rows(companion_db, AgentEvent)
        for turn in turns:
            events = [row for row in rows if row.turn_id == turn.id]
            assert {
                "companion_reflection",
                "companion_state",
                "context_snapshot",
                "model_request",
                "model_response",
            } <= {row.event_type for row in events}
            assert all(
                not row.model_visible
                for row in events
                if row.event_type
                in {
                    "companion_reflection",
                    "companion_state",
                    "context_snapshot",
                    "conversation_mode",
                    "companion_contact_decision",
                }
            )
            assert not any(row.event_type == "user_input" for row in events)
            assert sum(row.event_type == "model_request" for row in events) == 1
            assert sum(row.event_type == "model_response" for row in events) == 1
        assert (await store.read_companion_context(native_scope.id, "alpha", member.user_id))["life"]["plans"][0][
            "progress"
        ] == "Page 3"
        assert (await store.read_companion_context(native_scope.id, "alpha", member.user_id))["attention"] == [
            attention.model_dump(mode="json")
        ]
        assert await native_rows(companion_db, Conversation) == []
        assert await native_rows(companion_db, RelationshipEvidence) == []
        assert native_transport.network.wire == []
    finally:
        await worker.aclose()


@pytest.mark.asyncio
async def test_default_reflection_provider_failure_preserves_life_rearms_and_finishes_audit_without_retry(
    companion_db,
    native_transport,
    monkeypatch,
):
    native_scope = await scope(companion_db)
    member = await enable_member(companion_db, native_scope, at=NOW - timedelta(minutes=15))
    seeded_at = NOW - timedelta(minutes=10)
    attention = AttentionUpdate(
        member_ref=member.member_ref,
        key="exam-concern",
        kind="concern",
        summary="Alice is awaiting a real exam result",
        status="open",
        evidence_turn_ids=[member.last_turn_id],
    )
    claim = await store.claim_review(native_scope.id, "alpha", now=seeded_at)
    assert await store.finish_review(
        claim,
        ReflectionProposal(life=LifeState(focus="Existing ongoing life"), attention=[attention]),
        now=seeded_at,
        next_review_at=NOW,
    )
    payloads = install_provider(monkeypatch, [RuntimeError("The external provider is unavailable")])
    worker = CompanionService(config(), accounts=lambda: [native_transport.account], clock=Clock())
    try:
        await worker.start()
        await settled(worker)
        await settled(worker)
        assert len(payloads) == 1
        assert (await store.get_mode(native_scope.id)).mode == "companion"
        assert (await store.get_mode(native_scope.id)).next_review_at > NOW
        assert (await store.read_companion_context(native_scope.id, "alpha", member.user_id))["life"][
            "focus"
        ] == "Existing ongoing life"
        assert (await store.read_companion_context(native_scope.id, "alpha", member.user_id))["attention"] == [
            attention.model_dump(mode="json")
        ]
        turn = next(row for row in await native_rows(companion_db, AgentTurn) if row.trigger_kind == "reflection")
        assert turn.status == "failed"
        assert turn.finished_at is not None
        events = [row for row in await native_rows(companion_db, AgentEvent) if row.turn_id == turn.id]
        assert any(row.event_type == "companion_reflection" and row.status == "failed" for row in events)
        assert sum(row.event_type == "model_request" for row in events) == 1
        assert sum(row.event_type == "model_response" and row.status == "failed" for row in events) == 1
        assert native_transport.network.wire == []
    finally:
        await worker.aclose()


@pytest.mark.asyncio
async def test_default_reflection_unload_cancels_provider_and_drains_private_audit(
    companion_db, native_transport, monkeypatch
):
    native_scope = await scope(companion_db)
    member = await enable_member(companion_db, native_scope, at=NOW - timedelta(minutes=15))
    seeded_at = NOW - timedelta(minutes=10)
    attention = AttentionUpdate(
        member_ref=member.member_ref,
        key="exam-concern",
        kind="concern",
        summary="Alice is awaiting a real exam result",
        status="open",
        evidence_turn_ids=[member.last_turn_id],
    )
    claim = await store.claim_review(native_scope.id, "alpha", now=seeded_at)
    assert await store.finish_review(
        claim,
        ReflectionProposal(life=LifeState(focus="Existing ongoing life"), attention=[attention]),
        now=seeded_at,
        next_review_at=NOW,
    )
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def blocked_provider(payload):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    payloads = install_provider(monkeypatch, [blocked_provider])
    worker = CompanionService(config(), accounts=lambda: [native_transport.account], clock=Clock())
    try:
        await worker.start()
        await asyncio.wait_for(entered.wait(), 5)
        await asyncio.wait_for(worker.aclose(), 5)
        assert cancelled.is_set()
        assert len(payloads) == 1
        turn = next(row for row in await native_rows(companion_db, AgentTurn) if row.trigger_kind == "reflection")
        assert turn.status == "cancelled"
        assert turn.finished_at is not None
        events = [row for row in await native_rows(companion_db, AgentEvent) if row.turn_id == turn.id]
        assert any(row.event_type == "companion_reflection" and row.status == "cancelled" for row in events)
        assert sum(row.event_type == "model_request" for row in events) == 1
        assert sum(row.event_type == "model_response" and row.status == "cancelled" for row in events) == 1
        state = await store.read_companion_context(native_scope.id, "alpha", member.user_id)
        assert state["life"]["focus"] == "Existing ongoing life"
        assert state["attention"] == [attention.model_dump(mode="json")]
        assert all(row.status != "active" for row in await native_rows(companion_db, CompanionReview))
        assert await native_rows(companion_db, Conversation) == []
    finally:
        await worker.aclose()


def test_absent_legacy_episode_time_remains_explicitly_unknown():
    assert _episode_time(None, NOW) == {
        "created_at": None,
        "created_at_local": None,
        "age_minutes": None,
    }


@pytest.mark.asyncio
async def test_reflection_input_preserves_native_episode_times_ownership_and_whole_record_bounds(companion_db):
    native_scope = await scope(companion_db)
    alice_at = NOW - timedelta(hours=3)
    bob_at = NOW - timedelta(minutes=35)
    later_at = NOW - timedelta(minutes=20)
    alice = await enable_member(companion_db, native_scope, at=alice_at)
    bob = await enable_member(companion_db, native_scope, user="bob-unified", platform_user="platform-bob", at=bob_at)
    later = await completed_turn(
        companion_db, native_scope, user=alice.user_id, text="Please end this exchange for now.", at=later_at
    )
    assert await store.record_chat(
        native_scope.id,
        "alpha",
        user_id=alice.user_id,
        platform_user_id=alice.platform_user_id,
        display_name=alice.display_name,
        turn_id=later.id,
        now=later_at,
    )
    other_scope = await scope(companion_db, channel="unrelated-group")
    unrelated = await enable_member(companion_db, other_scope, at=NOW - timedelta(minutes=12))
    claim = await store.claim_review(native_scope.id, "alpha", now=NOW)
    snapshot = await store.load_reflection(claim, now=NOW)
    local_now = NOW.astimezone(timezone(timedelta(hours=8)))
    payload, ownership = await reflection_input(
        snapshot,
        scope_id=native_scope.id,
        channel_id=native_scope.channel_id,
        limits=companion_limits(config()),
        now=local_now,
        available_chars=config().companion_input_chars,
    )
    encoded = json.loads(build_reflection_prompt(payload, now=local_now).splitlines()[-1])
    members = {item["member_ref"]: item for item in encoded["members"]}
    assert ownership == {
        alice.member_ref: (alice.last_turn_id, later.id),
        bob.member_ref: (bob.last_turn_id,),
    }
    assert members[alice.member_ref]["last_user_age_minutes"] == 20
    assert [item["turn_id"] for item in members[alice.member_ref]["episodes"]] == [alice.last_turn_id, later.id]
    assert [item["turn_id"] for item in members[bob.member_ref]["episodes"]] == [bob.last_turn_id]
    expected = {
        alice.last_turn_id: (alice.user_id, alice_at, 180, "I am waiting for my exam result"),
        later.id: (alice.user_id, later_at, 20, "Please end this exchange for now."),
        bob.last_turn_id: (bob.user_id, bob_at, 35, "I am waiting for my exam result"),
    }
    episodes = {}
    for member_ref, item in members.items():
        assert item["known_evidence_turn_ids"] == list(ownership[member_ref])
        for episode in item["episodes"]:
            turn_id = episode["turn_id"]
            owner, finished_at, age, text = expected[turn_id]
            created_at = finished_at - timedelta(seconds=1)
            assert turn_id in ownership[member_ref]
            assert episode == {
                "turn_id": turn_id,
                "speaker": owner,
                "outcome": "completed",
                "created_at": created_at.isoformat(),
                "created_at_local": created_at.astimezone(local_now.tzinfo).isoformat(),
                "age_minutes": age,
                "user": text,
                "assistant": "I understand.",
            }
            episodes[turn_id] = episode
    assert set(episodes) == set(expected)
    assert unrelated.last_turn_id not in episodes
    budget = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"))) - 1
    bounded, bounded_ownership = await reflection_input(
        snapshot,
        scope_id=native_scope.id,
        channel_id=native_scope.channel_id,
        limits=companion_limits(config()),
        now=local_now,
        available_chars=budget,
    )
    assert bounded_ownership == ownership
    assert len(json.dumps(bounded, ensure_ascii=False, separators=(",", ":"))) <= budget
    retained = [episode for item in bounded["members"] for episode in item["episodes"]]
    assert 0 < len(retained) < len(episodes)
    assert all(episode == episodes[episode["turn_id"]] for episode in retained)


@pytest.mark.asyncio
async def test_native_reflections_distinguish_no_contact_silence_and_later_member_activity(
    companion_db, native_transport, monkeypatch
):
    native_scope = await scope(companion_db)
    member = await enable_member(companion_db, native_scope)
    proposal = ContactProposal(
        member_ref=member.member_ref,
        key="shared-color",
        purpose="free_chat",
        topic="Compare favorite colors",
        reason="A shared-interest conversation",
        evidence_turn_ids=[],
    )
    quiet = ReflectionProposal(life=LifeState(), review_after_minutes=10)
    payloads = install_provider(
        monkeypatch,
        [
            response(ReflectionProposal(life=LifeState(), contact=proposal, review_after_minutes=10).model_dump_json()),
            response(quiet.model_dump_json()),
            response(quiet.model_dump_json()),
        ],
    )
    clock = Clock()

    async def runner(session, ctx, prepared, **kwargs):
        message = "Which colors do you enjoy?"
        await send_with_delivery(session, message, prepared.delivery_state, texts=[message])
        await prepared.lifecycle.persist_delivered_text()
        return "completed"

    worker = CompanionService(config(), accounts=lambda: [native_transport.account], runner=runner, clock=clock)
    try:
        await worker.start()
        await settled(worker)
        clock.advance(minutes=10)
        await settled(worker)
        assert len(payloads) == 2
        clock.advance(minutes=1)
        ordinary = await completed_turn(
            companion_db, native_scope, user=member.user_id, text="An unrelated new topic", at=clock.now
        )
        assert await store.record_chat(
            native_scope.id,
            "alpha",
            user_id=member.user_id,
            platform_user_id=member.platform_user_id,
            display_name=member.display_name,
            turn_id=ordinary.id,
            now=clock.now,
        )
        clock.advance(minutes=9)
        await settled(worker)
        assert len(payloads) == 3
        facts = [json.loads(payload["messages"][1]["content"].splitlines()[-1])["members"][0] for payload in payloads]
        assert [item["contact_timing"] for item in facts] == [
            {"eligible": True, "block_reason": None, "contacts_today": 0, "activity_after_contact": None},
            {"eligible": True, "block_reason": None, "contacts_today": 1, "activity_after_contact": False},
            {"eligible": True, "block_reason": None, "contacts_today": 1, "activity_after_contact": True},
        ]
        assert [item["last_contact_age_minutes"] for item in facts] == [None, 10, 20]
        assert facts[-1]["episodes"][-1]["turn_id"] == ordinary.id
        assert facts[-1]["episodes"][-1]["user"] == "An unrelated new topic"
        assert len(native_transport.network.wire) == 1
    finally:
        await worker.aclose()


@pytest.mark.asyncio
async def test_reflection_keeps_ordered_supplements_in_one_owner_episode(companion_db):
    native_scope = await scope(companion_db)
    alice = await enable_member(companion_db, native_scope)
    bob = await enable_member(companion_db, native_scope, user="bob-unified", platform_user="platform-bob")
    existing = await native_rows(companion_db, AgentEvent)
    output = next(
        event for event in existing if event.turn_id == alice.last_turn_id and event.event_type == "assistant_output"
    )
    async with companion_db.factory() as db, db.begin():
        stored = await db.get(AgentEvent, output.id)
        stored.sequence = 10
        await db.flush()
        for sequence, event_type, text in (
            (2, "steer_received", "Private receipt, not applied text"),
            (3, "user_input", "I am also worried about tomorrow"),
            (4, "context_snapshot", "Other group members' ambient conversation"),
            (5, "user_input", "Please remember this clarification"),
            (6, "steer_received", "Still pending and not part of the interaction"),
        ):
            db.add(
                AgentEvent(
                    turn_id=alice.last_turn_id,
                    sequence=sequence,
                    event_type=event_type,
                    role="user",
                    model_visible=event_type == "user_input",
                    payload_json=json.dumps(
                        {
                            "content": json.dumps(
                                {
                                    "speaker": "Alice",
                                    "content": json.dumps(
                                        {"content": text, "forwarded_messages": [{"content": "Bob's quote"}]}
                                    ),
                                }
                            ),
                            "steer_sequence": 2 if sequence == 3 else 3,
                        }
                    ),
                )
            )
    claim = await store.claim_review(native_scope.id, "alpha", now=NOW)
    snapshot = await store.load_reflection(claim, now=NOW)
    payload, ownership = await reflection_input(
        snapshot,
        scope_id=native_scope.id,
        channel_id=native_scope.channel_id,
        limits=companion_limits(config()),
        now=NOW,
        available_chars=config().companion_input_chars,
    )
    members = {item["member_ref"]: item for item in payload["members"]}
    alice_episodes = members[alice.member_ref]["episodes"]
    assert len(alice_episodes) == 1
    assert alice_episodes[0]["turn_id"] == alice.last_turn_id
    assert alice_episodes[0]["user"] == (
        "I am waiting for my exam result\n\nI am also worried about tomorrow\n\nPlease remember this clarification"
    )
    assert members[bob.member_ref]["episodes"][0]["user"] == "I am waiting for my exam result"
    assert "Private receipt" not in json.dumps(payload)
    assert "ambient conversation" not in json.dumps(payload)
    assert "Bob's quote" not in json.dumps(payload)
    assert ownership[alice.member_ref] == (alice.last_turn_id,)


@pytest.mark.asyncio
@pytest.mark.parametrize("budget_delta", [0, -1])
async def test_native_reflection_reserves_all_member_attention_before_optional_observations(
    companion_db,
    native_transport,
    monkeypatch,
    budget_delta,
):
    native_scope = await scope(companion_db)
    alice = await enable_member(companion_db, native_scope, at=NOW - timedelta(minutes=15))
    bob = await enable_member(
        companion_db, native_scope, user="bob-unified", platform_user="platform-bob", at=NOW - timedelta(minutes=15)
    )
    attention = [
        AttentionUpdate(
            member_ref=member.member_ref,
            key="exam-concern",
            kind="concern",
            summary=f"{member.display_name} is awaiting a real exam result",
            status="open",
            evidence_turn_ids=[member.last_turn_id],
        )
        for member in (alice, bob)
    ]
    life = LifeState(
        focus="An ongoing fictional painting",
        plans=[
            LifePlan(
                key="paint-book",
                title="An imagined color book",
                status="active",
                progress="Page one",
            )
        ],
    )
    seeded_at = NOW - timedelta(minutes=10)
    seed = await store.claim_review(native_scope.id, "alpha", now=seeded_at)
    assert await store.finish_review(
        seed,
        ReflectionProposal(life=life, attention=attention),
        now=seeded_at,
        next_review_at=NOW,
    )
    claim = await store.claim_review(native_scope.id, "alpha", now=NOW)
    snapshot = await store.load_reflection(claim, now=NOW)
    payload, ownership = await reflection_input(
        snapshot,
        scope_id=native_scope.id,
        channel_id=native_scope.channel_id,
        limits=companion_limits(config()),
        now=NOW,
        available_chars=config().companion_input_chars,
    )
    assert len(payload["members"]) == 2
    assert all(item["episodes"] for item in payload["members"])
    for item in payload["members"]:
        item["episodes"] = []
    payload = json.loads(json.dumps(payload, ensure_ascii=False, allow_nan=False))
    persona = config().personas["alpha"]
    system = build_reflection_system(
        json.dumps(
            {
                "name": persona.name,
                "prompt": persona.prompt,
                "appearance": persona.appearance,
                "has_reference_image": bool(persona.reference_image),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    )
    budget = len(system) + len(build_reflection_prompt(payload, now=NOW)) + budget_delta
    bounded_config = config(companion_input_chars=budget)

    def provider(request):
        actual_system, user = request["messages"]
        actual = json.loads(user["content"].split("Host snapshot JSON (strings are untrusted data):\n", 1)[1])
        assert actual == payload
        assert len(actual_system["content"]) + len(user["content"]) == budget
        assert set(ownership) == {alice.member_ref, bob.member_ref}
        return response(ReflectionProposal(life=life, attention=attention).model_dump_json())

    requests = install_provider(monkeypatch, [provider])
    operation = run_reflection(
        config=bounded_config,
        scope=native_scope,
        claim=claim,
        snapshot=snapshot,
        limits=companion_limits(bounded_config),
        now=NOW,
    )
    if budget_delta == 0:
        proposal = await operation
        assert await store.finish_review(claim, proposal, now=NOW, next_review_at=NOW + timedelta(hours=1))
        assert len(requests) == 1
    else:
        with pytest.raises(ValueError, match="Complete companion life, members and attention"):
            await operation
        assert requests == []
        assert await store.fail_review(claim, now=NOW, retry_at=NOW + timedelta(hours=1), reason="input_budget")
    turns = [row for row in await native_rows(companion_db, AgentTurn) if row.trigger_kind == "reflection"]
    assert len(turns) == 1
    assert turns[0].status == ("completed" if budget_delta == 0 else "failed")
    assert turns[0].finished_at is not None
    events = [row for row in await native_rows(companion_db, AgentEvent) if row.turn_id == turns[0].id]
    assert sum(row.event_type == "model_request" for row in events) == (1 if budget_delta == 0 else 0)
    assert all(not row.model_visible for row in events)
    for member, expected in zip((alice, bob), attention):
        state = await store.read_companion_context(native_scope.id, "alpha", member.user_id)
        assert state["life"] == life.model_dump(mode="json")
        assert state["attention"] == [expected.model_dump(mode="json")]
    assert native_transport.network.wire == []


@pytest.mark.asyncio
async def test_task_free_native_send_and_finish_pause_continue_returns_actual_receipt(
    companion_db,
    native_transport,
    monkeypatch,
):
    native_scope = await scope(companion_db)
    member = await enable_member(companion_db, native_scope)
    message = "A color in my imagined sketchbook made me think of our conversation."
    payloads = install_provider(
        monkeypatch,
        [
            response(calls=[tool_call("send_msg", {"segments": [{"type": "text", "text": message}]}, "native-send")]),
            response(
                calls=[
                    tool_call(
                        "finish_turn", {"outcome": "delivered", "reason": "Native transport confirmed"}, "native-finish"
                    )
                ]
            ),
        ],
    )
    captured = []

    async def reflection(**kwargs):
        return ReflectionProposal(
            life=LifeState(focus="Continuing fictional painting"),
            contact=ContactProposal(
                member_ref=member.member_ref,
                key="color-conversation",
                purpose="free_chat",
                topic="Talk about imagined colors",
                reason="A natural shared-interest contact",
                evidence_turn_ids=[],
            ),
        )

    async def runner(session, ctx, prepared, **kwargs):
        captured.append(prepared)
        await generate_chat_response(
            prepared.chat_messages,
            system=prepared.system,
            model=kwargs["model_name"],
            channel_id=session.channel.id,
            ctx=ctx,
            web_limits=prepared.web_limits,
            delivery_state=prepared.delivery_state,
            image_inputs=prepared.image_inputs,
            media_intent=prepared.media_intent,
            tool_trace=prepared.lifecycle.tool_trace,
            agent_events=prepared.agent_events,
            agent_access=prepared.agent_access,
            resolution=prepared.resolution,
        )
        assert prepared.resolution.outcome == "delivered"
        await prepared.lifecycle.persist_delivered_text()
        return "completed"

    async with native_tools() as dispatcher:
        register_send_msg(dispatcher, SendMsgToolContext(no_mentions))
        register_finish_turn(dispatcher)
        register_reminder_tools(dispatcher)
        validator = Draft202012Validator(available_functions["send_msg"][1].parameters)
        assert list(validator.iter_errors({"segments": [{"type": "text", "text": message}]})) == []
        assert list(validator.iter_errors({"message": message}))
        worker = CompanionService(
            config(), accounts=lambda: [native_transport.account], reflection=reflection, runner=runner, clock=Clock()
        )
        try:
            await worker.start()
            await settled(worker)
            assert len(payloads) == 2
            names = {tool["function"]["name"] for tool in payloads[0]["tools"]}
            assert {"send_msg", "finish_turn"} <= names
            assert "reminder" not in names
            results = [item for item in payloads[1]["messages"] if item["role"] == "tool"]
            assert len(results) == 1
            result = json.loads(results[0]["content"])
            assert result["ok"] is True
            assert result["data"]["status"] == "delivered"
            assert result["data"]["messages"] == 1
            history = await store.read_contact_history(native_scope.id, "alpha")
            assert history[0]["status"] == "delivered"
            assert history[0]["receipts"] == ("native-receipt-1",)
            assert native_transport.network.wire[0]["channel_id"] == native_scope.channel_id
            assert len(native_transport.network.wire) == 1
            assert [
                (event.tool_name, event.status, event.effect) for event in captured[0].lifecycle.tool_trace.events
            ] == [
                ("send_msg", "succeeded", "confirmed"),
                ("finish_turn", "succeeded", "none"),
            ]
            assert captured[0].delivery_state.delivered_texts == [message]
            assert await native_rows(companion_db, RelationshipEvidence) == []
            assert await native_rows(companion_db, ReminderTask) == []
        finally:
            await worker.aclose()


@pytest.mark.asyncio
async def test_actual_prepared_autonomous_authority_rejects_reminder_and_artifact_writes_before_dispatch(
    companion_db,
    native_transport,
    monkeypatch,
    tmp_path,
):
    native_scope = await scope(companion_db)
    member = await enable_member(companion_db, native_scope)
    install_provider(monkeypatch, [])
    prepared = await prepare_contact(native_transport.session(), member)
    artifacts = WebArtifactService(tmp_path / "artifacts", public_origin="https://preview.invalid")
    monkeypatch.setattr(reminder_store, "get_session", companion_db.factory)
    try:
        async with native_tools() as dispatcher:
            register_send_msg(dispatcher, SendMsgToolContext(no_mentions))
            register_finish_turn(dispatcher)
            register_reminder_tools(dispatcher)
            register_artifact_tools(
                dispatcher, config(web_artifacts_public_url="https://preview.invalid"), service=artifacts
            )
            blocked = [
                requirement("reminder", {"request": {"action": "preferences", "enabled": False}}, "disable-preference"),
                requirement(
                    "publish_web_preview",
                    {
                        "title": "Forbidden publication",
                        "source_files": [{"path": "index.html", "content": "<p>Not authorized</p>"}],
                    },
                    "publish-write",
                ),
                requirement("revoke_web_preview", {"artifact_ref": "invented-private-artifact"}, "revoke-write"),
            ]
            assert prepared.agent_access.raw_user_text == ""
            assert not prepared.agent_access.is_operator
            assert not prepared.agent_access.allow_archived_sessions
            assert not prepared.agent_access.allow_context_pin
            assert not prepared.agent_access.allow_payload_delivery
            with (
                agent_access_scope(prepared.agent_access),
                llm_chat_delivery_scope(prepared.delivery_state),
                llm_chat_tool_trace_scope(prepared.lifecycle.tool_trace),
                llm_chat_context_scope(Contexts({ITEM_SESSION: native_transport.session()})),
            ):
                advertised = {function.name for function in llm_service.get_agno_tools()}
                assert {"send_msg", "finish_turn"} <= advertised
                assert not {"reminder", "publish_web_preview", "revoke_web_preview"} & advertised
                await llm_service.run_llm_tools(RunOutput(requirements=blocked))
            for item in blocked:
                outcome = json.loads(item.external_execution_result)
                assert outcome["ok"] is False
                assert "not allowed" in outcome["error"]
                assert item.tool_execution.tool_call_error
            assert await native_rows(companion_db, ReminderTask) == []
            assert await native_rows(companion_db, ReminderPreference) == []
            assert (
                await artifacts.list_owned(ArtifactOwner(prepared.agent_access.scope_id, prepared.agent_access.user_id))
                == []
            )
            assert native_transport.network.wire == []
            assert prepared.delivery_state.delivery_attempts == 0
    finally:
        await prepared.lifecycle.finalize_agent_turn("cancelled")
        await prepared.image_inputs.aclose()
        await artifacts.close()


@pytest.mark.asyncio
async def test_native_finish_silent_sends_nothing_and_does_not_recover_visible_text(
    companion_db, native_transport, monkeypatch
):
    native_scope = await scope(companion_db)
    member = await enable_member(companion_db, native_scope)
    payloads = install_provider(
        monkeypatch,
        [
            response(
                calls=[
                    tool_call(
                        "finish_turn",
                        {"outcome": "silent", "reason": "No natural contact is appropriate now"},
                        "native-silent",
                    )
                ]
            )
        ],
    )
    captured = []

    async def reflection(**kwargs):
        return ReflectionProposal(
            life=LifeState(),
            contact=ContactProposal(
                member_ref=member.member_ref,
                key="quiet-consideration",
                purpose="free_chat",
                topic="Consider a relaxed conversation",
                reason="A possible shared interest",
                evidence_turn_ids=[],
            ),
        )

    async def runner(session, ctx, prepared, **kwargs):
        captured.append(prepared)
        await generate_chat_response(
            prepared.chat_messages,
            system=prepared.system,
            model=kwargs["model_name"],
            channel_id=session.channel.id,
            ctx=ctx,
            web_limits=prepared.web_limits,
            delivery_state=prepared.delivery_state,
            image_inputs=prepared.image_inputs,
            media_intent=prepared.media_intent,
            tool_trace=prepared.lifecycle.tool_trace,
            agent_events=prepared.agent_events,
            agent_access=prepared.agent_access,
            resolution=prepared.resolution,
        )
        assert prepared.resolution.outcome == "silent"
        return "silent"

    async with native_tools() as dispatcher:
        register_finish_turn(dispatcher)
        worker = CompanionService(
            config(), accounts=lambda: [native_transport.account], reflection=reflection, runner=runner, clock=Clock()
        )
        try:
            await worker.start()
            await settled(worker)
            assert len(payloads) == 1
            assert native_transport.network.wire == []
            history = await store.read_contact_history(native_scope.id, "alpha")
            assert history[0]["status"] == "silent"
            assert history[0]["receipts"] == ()
            assert captured[0].delivery_state.delivery_attempts == 0
        finally:
            await worker.aclose()


@pytest.mark.asyncio
async def test_normal_reminder_creation_and_scheduled_resolution_keep_native_contract_after_mode_off(
    companion_db,
    native_transport,
    monkeypatch,
):
    native_scope = await scope(companion_db)
    await store.set_mode(native_scope.id, "companion", persona_key="alpha", now=NOW)
    await store.set_mode(native_scope.id, "normal", persona_key="alpha", now=NOW)
    install_provider(monkeypatch, [])
    monkeypatch.setattr(reminder_store, "get_session", companion_db.factory)
    session = native_transport.session()
    inputs = ImageInputs()
    normal = await agent_turn_setup.prepare_agent_turn(
        config(),
        session,
        ChatIdentity("alice-unified", "Alice", ""),
        model_name="test-model",
        model_text="Please remind me tomorrow",
        raw_user_text="Please remind me tomorrow",
        content="Please remind me tomorrow",
        current_content=None,
        image_inputs=inputs,
        forwarded_messages=(),
        mentioned_participants=(),
        warn=pytest.fail,
        tool_schemas=(),
    )
    scheduled = None
    try:
        async with native_tools() as dispatcher:
            register_reminder_tools(dispatcher)
            due = datetime.now(timezone.utc) + timedelta(days=1)
            create = requirement(
                "reminder",
                {
                    "request": {
                        "action": "create",
                        "goal": "Review the explicit project",
                        "schedule": due.isoformat(),
                        "timezone": "UTC",
                    }
                },
                "explicit-create",
            )
            with (
                agent_access_scope(normal.agent_access),
                llm_chat_delivery_scope(normal.delivery_state),
                llm_chat_tool_trace_scope(normal.lifecycle.tool_trace),
                llm_chat_context_scope(Contexts({ITEM_SESSION: session})),
            ):
                await llm_service.run_llm_tools(RunOutput(requirements=[create]))
            result = json.loads(create.external_execution_result)
            assert result["ok"] is True, result
            task_ref = json.loads(result["data"])["task_ref"]
            tasks = await native_rows(companion_db, ReminderTask)
            assert len(tasks) == 1
            assert tasks[0].goal == "Review the explicit project"
            scheduled = await agent_turn_setup.prepare_scheduled_agent_turn(
                config(),
                session,
                ChatIdentity("alice-unified", "Alice", ""),
                model_name="test-model",
                task_ref=task_ref,
                content=tasks[0].goal,
                scheduled_context={"mode": "once", "now": NOW.isoformat()},
                image_inputs=ImageInputs(),
                warn=pytest.fail,
                tool_schemas=(),
            )
            resolve = requirement(
                "reminder", {"request": {"action": "resolve", "decision": "completed"}}, "explicit-resolve"
            )
            with (
                agent_access_scope(scheduled.agent_access),
                llm_chat_delivery_scope(scheduled.delivery_state),
                llm_chat_tool_trace_scope(scheduled.lifecycle.tool_trace),
                llm_chat_context_scope(Contexts({ITEM_SESSION: session})),
                reminder_decision_scope(),
            ):
                await llm_service.run_llm_tools(RunOutput(requirements=[resolve]))
                assert json.loads(resolve.external_execution_result)["ok"] is True
                assert current_reminder_decision().task_ref == task_ref
                assert current_reminder_decision().status == "completed"
            assert normal.conversation_mode == "normal"
            assert normal.agent_access.capability_names == frozenset({"reminder"})
            assert scheduled.agent_access.trigger_kind == "scheduled"
            assert scheduled.agent_access.raw_user_text == ""
            assert scheduled.agent_access.reminder_task_ref == task_ref
            assert scheduled.lifecycle.user_message_id is None
            assert (await store.get_mode(native_scope.id)).mode == "normal"
            assert await store.read_contact_history(native_scope.id, "alpha") == ()
    finally:
        await normal.lifecycle.finalize_agent_turn("completed")
        await normal.image_inputs.aclose()
        if scheduled is not None:
            await scheduled.lifecycle.finalize_agent_turn("completed")
            await scheduled.image_inputs.aclose()
