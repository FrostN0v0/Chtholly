"""Scheduled generation is isolated from inbound user turns and relationship evidence."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import cast
from pathlib import Path

import pytest
from sqlalchemy import text, select
from arclet.entari import Session
from arclet.entari.config import EntariConfig
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

if not hasattr(EntariConfig, "instance"):
    setattr(
        EntariConfig, "instance", EntariConfig.load(Path(__file__).resolve().parents[1] / "entari.full.example.yml")
    )

from entari_plugin_database import Base

from plugins.llm_chat import (
    personality,
    agent_events,
    context_builder,
    session_manager,
    agent_turn_setup,
    scheduled_chat_context,
)
from plugins.llm_chat.config import LLMChatConfig, PersonaConfig
from plugins.llm_chat.models import (
    AgentTurn,
    AgentEvent,
    Conversation,
    UserRelation,
    ContextSession,
    MigrationState,
    RelationshipEvidence,
)
from plugins.llm_chat.identity import ChatIdentity
from plugins.llm_chat.image_inputs import ImageInputs
from plugins.llm_chat.relationships import evidence, recovery
from plugins.llm_chat.session_manager import ScopeIdentity, BaselineFingerprint
from plugins.llm_chat.relationships.state import snapshot_relationship
from plugins.llm_chat.persona.memory_context import MemoryContext


@pytest.fixture
async def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'scheduled.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    for module in (agent_events, context_builder, personality, session_manager, evidence, recovery):
        monkeypatch.setattr(module, "get_session", factory)
    try:
        yield factory
    finally:
        await engine.dispose()


def _baseline() -> BaselineFingerprint:
    return BaselineFingerprint("test", "persona", "system", "tools", "policy")


async def _add_completed_chat_turn(store, context_session, *, user: str, assistant: str, speaker: str = "Alice"):
    turn = await session_manager.start_turn(
        context_session,
        trigger_message_id="inbound",
        user_id="alice",
        user_name=speaker,
        conversation_user_id=None,
        fresh_context=False,
    )
    async with store() as db:
        db.add_all(
            [
                AgentEvent(
                    turn_id=turn.id,
                    sequence=1,
                    event_type="user_input",
                    role="user",
                    payload_json=json.dumps(
                        {"content": json.dumps({"speaker": speaker, "content": user}, ensure_ascii=False)},
                        ensure_ascii=False,
                    ),
                    model_visible=True,
                ),
                AgentEvent(
                    turn_id=turn.id,
                    sequence=2,
                    event_type="assistant_output",
                    role="assistant",
                    payload_json=json.dumps({"content": assistant}, ensure_ascii=False),
                    status="confirmed",
                    effect="confirmed",
                    model_visible=True,
                ),
            ]
        )
        await db.commit()
    await session_manager.finish_turn(turn.id, status="completed", final_text=assistant)
    return turn


@pytest.mark.asyncio
async def test_scheduled_snapshot_reads_only_destination_active_chat_scope(store):
    group_scope = await session_manager.get_or_create_scope(ScopeIdentity("test", "bot", "guild", "group", "group"))
    dm_scope = await session_manager.get_or_create_scope(ScopeIdentity("test", "bot", "", "dm", "dm"))
    group_chat, _ = await session_manager.ensure_active_session(group_scope, _baseline(), idle_minutes=60, max_turns=20)
    dm_chat, _ = await session_manager.ensure_active_session(dm_scope, _baseline(), idle_minutes=60, max_turns=20)
    await _add_completed_chat_turn(store, group_chat, user="group secret", assistant="group answer")
    dm_turn = await _add_completed_chat_turn(
        store,
        dm_chat,
        user="dm question media_0123456789abcdef0123456789abcdef https://c2cpicdw.qpic.cn/private.png",
        assistant="dm answer",
    )

    snapshot = await scheduled_chat_context.read_scheduled_chat_snapshot(dm_scope.id)

    assert snapshot is not None
    assert snapshot.source_session_ref == dm_chat.session_ref
    assert snapshot.selected_turn_refs == (dm_turn.turn_ref,)
    rendered_turns = snapshot.model_payload["turns"]
    assert [item["speaker"] for item in rendered_turns] == ["Alice", "assistant"]
    assert "dm question" in rendered_turns[0]["content"]
    assert "group secret" not in json.dumps(snapshot.model_payload, ensure_ascii=False)
    rendered = json.dumps(snapshot.model_payload, ensure_ascii=False)
    assert "media_0123456789abcdef0123456789abcdef" not in rendered
    assert "c2cpicdw.qpic.cn/private.png" not in rendered


@pytest.mark.asyncio
async def test_scheduled_snapshot_is_absent_without_active_chat_session(store):
    scope = await session_manager.get_or_create_scope(ScopeIdentity("test", "bot", "guild", "channel", "channel"))
    await session_manager.ensure_active_session(
        scope, _baseline(), idle_minutes=60, max_turns=20, purpose="reminder", reminder_task_ref="task-a"
    )

    assert await scheduled_chat_context.read_scheduled_chat_snapshot(scope.id) is None


@pytest.mark.asyncio
async def test_scheduled_snapshot_drops_old_chat_after_hard_reset_and_caps_text(store):
    scope = await session_manager.get_or_create_scope(ScopeIdentity("test", "bot", "guild", "channel", "channel"))
    old_chat, _ = await session_manager.ensure_active_session(scope, _baseline(), idle_minutes=60, max_turns=20)
    old_turn = await _add_completed_chat_turn(store, old_chat, user="old sealed text", assistant="old answer")
    current_chat = await session_manager.rollover_session(scope, old_chat, _baseline(), reason="manual_reset")
    await _add_completed_chat_turn(store, current_chat, user="x" * 500, assistant="y" * 500)

    snapshot = await scheduled_chat_context.read_scheduled_chat_snapshot(scope.id, max_chars=512)

    assert snapshot is not None
    assert snapshot.source_session_ref == current_chat.session_ref
    assert old_turn.turn_ref not in snapshot.selected_turn_refs
    assert snapshot.selected_turn_refs
    assert sum(len(item["content"]) for item in snapshot.model_payload["turns"]) <= 512


@pytest.mark.asyncio
async def test_chat_and_independent_task_sessions_coexist_without_chat_reset_sealing_them(store):
    scope = await session_manager.get_or_create_scope(ScopeIdentity("test", "bot", "guild", "channel", "channel"))
    chat, _ = await session_manager.ensure_active_session(scope, _baseline(), idle_minutes=60, max_turns=20)
    first, _ = await session_manager.ensure_active_session(
        scope, _baseline(), idle_minutes=60, max_turns=20, purpose="reminder", reminder_task_ref="task-a"
    )
    second, _ = await session_manager.ensure_active_session(
        scope, _baseline(), idle_minutes=60, max_turns=20, purpose="reminder", reminder_task_ref="task-b"
    )
    assert len({chat.id, first.id, second.id}) == 3
    assert (await session_manager.get_active_session(scope.id)).id == chat.id
    assert (
        await session_manager.get_active_session(scope.id, purpose="reminder", reminder_task_ref="task-a")
    ).id == first.id
    assert [row.id for row in await session_manager.list_scope_sessions(scope.id)] == [chat.id]
    assert await session_manager.get_session_by_ref(first.session_ref) is None
    assert (await session_manager.get_session_by_ref(first.session_ref, purpose="reminder")).id == first.id
    with pytest.raises(ValueError, match="purpose"):
        await session_manager.rollover_session(scope, first, _baseline(), reason="manual_reset")
    await session_manager.seal_scope_sessions(scope.id)
    assert (
        await session_manager.get_active_session(scope.id, purpose="reminder", reminder_task_ref="task-a")
    ).id == first.id
    continued = await session_manager.rollover_session(
        scope, first, _baseline(), reason="turn_limit", purpose="reminder"
    )
    assert continued.reminder_task_ref == "task-a"
    assert (
        await session_manager.get_active_session(scope.id, purpose="reminder", reminder_task_ref="task-b")
    ).id == second.id


@pytest.mark.asyncio
async def test_scheduled_prepare_captures_task_not_user_input_and_normal_prepare_remains_compatible(
    store, monkeypatch: pytest.MonkeyPatch
):
    async def relation(*_args):
        row = UserRelation(
            user_id="alice",
            channel_id="channel",
            impression="",
            affection=30.0,
            trust=30.0,
            dependence=0.0,
            resentment=0.0,
            familiarity=0.0,
        )
        return row, snapshot_relationship(row, None)

    async def mood(*_args):
        return 0.0

    async def memory(*_args):
        return MemoryContext(chat_profile={}, evaluator_profile_facts=[], relevant_memories=[], retrieval={})

    async def append(*args):
        async with store() as db:
            row = Conversation(channel_id=args[0], user_id=args[1], user_name=args[2], role=args[3], content=args[4])
            db.add(row)
            await db.commit()
            return row.id

    deleted: list[int] = []

    async def delete(row_id: int):
        deleted.append(row_id)

    for name, value in (
        ("load_relationship_snapshot", relation),
        ("get_mood", mood),
        ("load_memory_context", memory),
        ("append_message", append),
        ("delete_message", delete),
    ):
        monkeypatch.setattr(agent_turn_setup, name, value)
    config = LLMChatConfig(
        memory_enabled=False,
        default_persona="pepe",
        personas={"pepe": PersonaConfig(name="Pepe", prompt="Test persona")},
    )
    session = cast(
        Session,
        SimpleNamespace(
            account=SimpleNamespace(platform="test", self_id="bot"),
            channel=SimpleNamespace(id="channel", name="Test Channel"),
            guild=SimpleNamespace(id="guild", name="Test Guild"),
            event=SimpleNamespace(message=SimpleNamespace(id="real-message")),
        ),
    )
    identity = ChatIdentity(user_id="alice", display_name="Alice", participant_ref="participant_alice")
    destination_scope = await session_manager.get_or_create_scope(
        ScopeIdentity("test", "bot", "guild", "channel", "channel")
    )
    destination_chat, _ = await session_manager.ensure_active_session(
        destination_scope, _baseline(), idle_minutes=60, max_turns=20
    )
    destination_turn = await _add_completed_chat_turn(
        store, destination_chat, user="What did I ask you to remember?", assistant="The destination chat context."
    )
    prepared = await agent_turn_setup.prepare_scheduled_agent_turn(
        config,
        session,
        identity,
        model_name="test",
        task_ref="task-a",
        content="Check the progress tomorrow",
        scheduled_context={
            "occurrence_sequence": 2,
            "mode": "once",
            "scheduled_at": "2026-09-23T12:00:00Z",
            "now": "2026-09-23T12:01:00Z",
            "late_by_seconds": 60,
            "media_ref": "secret",
        },
        image_inputs=ImageInputs(),
        warn=lambda _: None,
        tool_schemas=[],
    )
    assert prepared.lifecycle.user_message_id is None
    assert prepared.agent_access.raw_user_text == ""
    assert prepared.agent_access.trigger_kind == "scheduled"
    assert prepared.agent_access.is_operator is False
    task = json.loads(prepared.chat_messages[-1]["content"].split(": ", 1)[1])
    ordinary = task["read_only_ordinary_chat_context"]
    assert ordinary["label"] == "UNTRUSTED_READ_ONLY_ORDINARY_CHAT_CONTEXT"
    assert [item["speaker"] for item in ordinary["turns"]] == ["Alice", "assistant"]
    assert "What did I ask you to remember?" in ordinary["turns"][0]["content"]
    assert task["goal"] == "Check the progress tomorrow"
    assert task["occurrence_sequence"] == 2
    assert task["late_by_seconds"] == 60
    assert task["mode"] == "once"
    assert '"action":"resolve"' in prepared.system
    assert "media_ref" not in task
    await prepared.lifecycle.rollback_if_unstarted()
    assert deleted == []
    async with store() as db:
        scheduled_turn = await db.get(AgentTurn, prepared.lifecycle.agent_turn_id)
        context = await db.get(ContextSession, scheduled_turn.session_id)
        started = list(
            (
                await db.execute(
                    select(AgentEvent).where(AgentEvent.turn_id == scheduled_turn.id).order_by(AgentEvent.sequence)
                )
            ).scalars()
        )
        users = list((await db.execute(select(Conversation).where(Conversation.role == "user"))).scalars())
    assert scheduled_turn.trigger_kind == "scheduled"
    assert scheduled_turn.trigger_message_id == ""
    assert scheduled_turn.conversation_user_id is None
    assert context.purpose == "reminder"
    assert context.reminder_task_ref == "task-a"
    assert users == []
    assert started[0].event_type == "reminder_trigger"
    assert started[0].model_visible is False
    assert "media_ref" not in started[0].payload_json
    assert all(event.event_type != "user_input" for event in started)
    context_snapshot_payload = json.loads(
        next(event.payload_json for event in started if event.event_type == "context_snapshot")
    )
    assert context_snapshot_payload["messages"] == prepared.chat_messages
    assert context_snapshot_payload["selection"]["ordinary_chat"] == {
        "status": "present",
        "session_ref": destination_chat.session_ref,
        "included_turn_refs": [destination_turn.turn_ref],
    }

    normal = await agent_turn_setup.prepare_agent_turn(
        config,
        session,
        identity,
        model_name="test",
        model_text="hello",
        raw_user_text="hello",
        content="hello",
        current_content=None,
        forwarded_messages=(),
        mentioned_participants=(),
        image_inputs=ImageInputs(),
        warn=lambda _: None,
        tool_schemas=[],
    )
    assert normal.lifecycle.user_message_id is not None
    assert '"action":"resolve"' not in normal.system
    assert normal.agent_access.capability_names == frozenset({"reminder"})
    async with store() as db:
        turn = await db.get(AgentTurn, normal.lifecycle.agent_turn_id)
        context = await db.get(ContextSession, turn.session_id)
        events = list(
            (
                await db.execute(select(AgentEvent).where(AgentEvent.turn_id == turn.id).order_by(AgentEvent.sequence))
            ).scalars()
        )
        users = list((await db.execute(select(Conversation).where(Conversation.role == "user"))).scalars())
    assert (context.purpose, turn.trigger_kind, turn.trigger_message_id, turn.conversation_user_id) == (
        "chat",
        "message",
        "real-message",
        normal.lifecycle.user_message_id,
    )
    assert [event.event_type for event in events if event.event_type in {"user_input", "reminder_trigger"}] == [
        "user_input"
    ]
    assert len(users) == 1


@pytest.mark.asyncio
async def test_scheduled_evidence_never_enqueued_even_with_legacy_user_event(store):
    scope = await session_manager.get_or_create_scope(ScopeIdentity("test", "bot", "guild", "channel", "channel"))
    context = await session_manager.create_session(
        scope.id, _baseline(), start_reason="task", purpose="reminder", reminder_task_ref="task-a"
    )
    turn = await session_manager.start_turn(
        context,
        trigger_message_id="",
        user_id="alice",
        user_name="Alice",
        conversation_user_id=None,
        fresh_context=False,
        trigger_kind="scheduled",
    )
    async with store() as db:
        db.add(
            MigrationState(
                key="relationship_affect_v1", status="completed", details_json=json.dumps({"cutover_turn_id": 0})
            )
        )
        db.add_all(
            [
                AgentEvent(turn_id=turn.id, sequence=1, event_type="user_input", payload_json='{"content":"fake"}'),
                AgentEvent(
                    turn_id=turn.id,
                    sequence=2,
                    event_type="response_decision",
                    payload_json='{"outcome":"automatic","actual_delivery":{"text_messages":1,"media_messages":0,"confirmed_deliveries":1}}',
                ),
                AgentEvent(
                    turn_id=turn.id,
                    sequence=3,
                    event_type="context_snapshot",
                    payload_json='{"persona":{"prompt":"test"}}',
                ),
            ]
        )
        await db.commit()
    await session_manager.finish_turn(turn.id, status="completed", final_text="delivered")
    assert (
        await evidence.enqueue_relationship_evidence(
            turn_id=turn.id, user_id="alice", channel_id="channel", persona_prompt="test", model="test"
        )
        is False
    )
    assert await recovery.recover_unqueued_evidence("test") == 0
    async with store() as db:
        assert list((await db.execute(select(RelationshipEvidence))).scalars()) == []
        assert (
            list(
                (
                    await db.execute(select(AgentEvent).where(AgentEvent.event_type == "relationship_evaluation"))
                ).scalars()
            )
            == []
        )


@pytest.mark.asyncio
async def test_explicit_column_migration_backfills_old_rows_and_is_repeatable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(session_manager, "get_session", factory)
    async with engine.begin() as db:
        await db.execute(text("CREATE TABLE chat_context_sessions (id INTEGER PRIMARY KEY, status VARCHAR)"))
        await db.execute(text("CREATE TABLE chat_agent_turns (id INTEGER PRIMARY KEY, status VARCHAR)"))
        await db.execute(text("INSERT INTO chat_context_sessions (status) VALUES ('active')"))
        await db.execute(text("INSERT INTO chat_agent_turns (status) VALUES ('completed')"))
    try:
        await session_manager.migrate_turn_origin_columns()
        await session_manager.migrate_turn_origin_columns()
        async with factory() as db:
            assert (await db.execute(text("SELECT purpose, reminder_task_ref FROM chat_context_sessions"))).one() == (
                "chat",
                "",
            )
            assert (await db.execute(text("SELECT trigger_kind FROM chat_agent_turns"))).scalar_one() == "message"
    finally:
        await engine.dispose()
