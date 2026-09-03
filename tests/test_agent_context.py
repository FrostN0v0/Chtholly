"""Behavioral contracts for AgentEvent context sessions."""

from __future__ import annotations

import json
from types import SimpleNamespace
import base64
from typing import Any, cast
import asyncio
from hashlib import sha256
from pathlib import Path
from datetime import datetime, timezone, timedelta
from dataclasses import replace

import pytest
from sqlalchemy import select
from arclet.entari import Image, Session
from arclet.entari.config import EntariConfig

if not hasattr(EntariConfig, "instance"):
    setattr(EntariConfig, "instance", EntariConfig.load(Path(__file__).resolve().parents[1] / "entari.yml"))

from entari_plugin_database import Base
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from plugins.llm_chat import (
    generation,
    agent_query,
    agent_events,
    agent_migration,
    context_builder,
    session_handoff,
    session_manager,
    agent_turn_setup,
    engagement_state,
    agent_attachments,
)
from plugins.llm_chat.core import tool_trace
from plugins.llm_chat.config import LLMChatConfig
from plugins.llm_chat.models import (
    AgentTurn,
    ChatScope,
    AgentEvent,
    Conversation,
    UserRelation,
    ToolExecution,
    ContextSession,
    MigrationState,
)
from plugins.llm_chat.identity import ChatIdentity
from plugins.llm_chat.agent_admin import AgentAdminService
from plugins.llm_chat.agent_events import load_event_payload
from plugins.llm_chat.agent_context import AgentAccessContext
from plugins.llm_chat.core.tool_trace import ToolTraceRecorder
from plugins.llm_chat.session_manager import ScopeIdentity, BaselineFingerprint
from plugins.llm_chat.agent_event_view import serialize_event_view
from plugins.llm_chat.core.agent_trace import AgentTurnRecorder
from plugins.llm_chat.core.tool_trace_policy import DeliverySnapshot
from plugins.llm_chat.persona.memory_context import MemoryContext


@pytest.fixture
async def agent_store(monkeypatch: pytest.MonkeyPatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    for module in (agent_events, agent_migration, agent_query, session_manager, engagement_state):
        monkeypatch.setattr(module, "get_session", session_factory)
    try:
        yield SimpleNamespace(engine=engine, session_factory=session_factory)
    finally:
        await engine.dispose()


def _baseline(model: str = "test-model") -> BaselineFingerprint:
    return BaselineFingerprint(
        model_name=model,
        persona_hash="persona-v1",
        system_version="system-v1",
        tool_schema_hash="tools-v1",
        policy_version="policy-v1",
    )


async def _scope_and_session() -> tuple[ChatScope, ContextSession]:
    scope = await session_manager.get_or_create_scope(
        ScopeIdentity(
            platform="test",
            account_id="bot",
            guild_id="guild",
            channel_id="channel",
            display_name="Test Channel",
        )
    )
    context_session = await session_manager.create_session(scope.id, _baseline(), start_reason="initial")
    return scope, context_session


@pytest.mark.asyncio
async def test_agent_event_rebuild_preserves_tool_pair_and_externalizes_large_source(
    agent_store: SimpleNamespace,
) -> None:
    scope, context_session = await _scope_and_session()
    turn = await session_manager.start_turn(
        context_session,
        trigger_message_id="message-1",
        user_id="alice",
        user_name="Alice",
        conversation_user_id=None,
        fresh_context=False,
    )
    source = "<html>\n" + "  <div>exact spacing</div>\n" * 30 + "</html>"
    trace = ToolTraceRecorder()
    trace.set_attempt(2)
    call = trace.start("html2pic", {"html": source, "width": 900})
    trace.finish_success(
        call,
        {"rendered": True, "width": 900},
        before=DeliverySnapshot(),
        after=DeliverySnapshot(),
    )
    recorder = AgentTurnRecorder()
    recorder.record_user_input('{"speaker":"Alice","content":"render it"}', user_name="Alice", fresh_context=False)
    recorder.record_tool_events(trace.events)
    recorder.record_assistant_output("Rendered.")
    await agent_events.persist_agent_events(turn.id, recorder.events)
    await session_manager.finish_turn(turn.id, status="completed", final_text="Rendered.")

    rows = await agent_events.load_turn_events(turn.id)
    assert [row.event_type for row in rows] == [
        "user_input",
        "assistant_tool_call",
        "tool_result",
        "assistant_output",
    ]
    call_event = rows[1]
    result_event = rows[2]
    payload = load_event_payload(call_event)
    stored_arguments = cast(dict[str, object], payload["arguments"])
    assert stored_arguments["html"] == source
    assert call_event.execution_ref == result_event.execution_ref == call.execution_ref
    assert call_event.tool_call_id == result_event.tool_call_id == call.execution_ref
    assert call_event.attempt == result_event.attempt == 2
    selection = await context_builder.select_session_context(
        context_session,
        system="system",
        current_message={"role": "user", "content": "next"},
        model_name="test-model",
        max_input_tokens=100_000,
        output_reserve_tokens=1000,
        rollover_ratio=0.9,
        minimum_recent_turns=1,
        inline_event_chars=16,
        fresh_context=False,
    )
    assert [message["role"] for message in selection.messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "user",
    ]
    assistant_call = cast(dict[str, Any], selection.messages[1])
    tool_result = cast(dict[str, Any], selection.messages[2])
    tool_calls = cast(list[dict[str, Any]], assistant_call.get("tool_calls"))
    tool_call = tool_calls[0]
    assert tool_call["id"] == tool_result["tool_call_id"] == call.execution_ref
    arguments = json.loads(tool_call["function"]["arguments"])
    descriptor = arguments["html"]
    assert descriptor["stored"] is True
    assert descriptor["event_ref"] == call_event.event_ref
    assert descriptor["path"] == "arguments.html"
    assert descriptor["chars"] == len(json.dumps(source, ensure_ascii=False, separators=(",", ":")))
    assert (
        descriptor["sha256"]
        == sha256(json.dumps(source, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
    )
    execution = await agent_query.read_tool_execution_payload(
        AgentAccessContext(
            scope.id,
            context_session.id,
            turn.id,
            "alice",
            allow_payload_delivery=True,
        ),
        execution_ref=call.execution_ref,
        path="arguments.html",
        max_chars=2000,
    )
    assert execution["data"] == source
    assert execution["call_event_ref"] == call_event.event_ref
    assert execution["result_event_ref"] == result_event.event_ref


@pytest.mark.asyncio
async def test_context_budget_drops_whole_old_turns_without_orphaning_tool_messages(
    agent_store: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _scope, context_session = await _scope_and_session()
    for index in range(3):
        turn = await session_manager.start_turn(
            context_session,
            trigger_message_id=f"message-{index}",
            user_id="alice",
            user_name="Alice",
            conversation_user_id=None,
            fresh_context=False,
        )
        trace = ToolTraceRecorder()
        call = trace.start("web_search", {"query": f"query-{index}"})
        trace.finish_success(
            call,
            {"results": ["x" * 120]},
            before=DeliverySnapshot(),
            after=DeliverySnapshot(),
        )
        recorder = AgentTurnRecorder()
        recorder.record_user_input(f"turn-{index}-" + "u" * 120, user_name="Alice", fresh_context=False)
        recorder.record_tool_events(trace.events)
        recorder.record_assistant_output(f"reply-{index}-" + "a" * 120)
        await agent_events.persist_agent_events(turn.id, recorder.events)
        await session_manager.finish_turn(turn.id, status="completed", final_text=f"reply-{index}")

    def count_messages(_model: str | None, messages: list[dict[str, object]]) -> int:
        return len(json.dumps(messages, ensure_ascii=False))

    monkeypatch.setattr(context_builder, "estimate_tokens", count_messages)
    selection = await context_builder.select_session_context(
        context_session,
        system="system",
        current_message={"role": "user", "content": "current"},
        model_name="test-model",
        max_input_tokens=1200,
        output_reserve_tokens=100,
        rollover_ratio=0.75,
        minimum_recent_turns=1,
        inline_event_chars=4000,
        fresh_context=False,
    )

    assert selection.excluded_turn_refs
    assert selection.rollover_required is True
    roles = [message["role"] for message in selection.messages]
    for index, role in enumerate(roles):
        if role == "tool":
            assert index > 0
            assert roles[index - 1] == "assistant"
            assert selection.messages[index - 1].get("tool_calls")


@pytest.mark.asyncio
async def test_session_rollover_reasons_and_hard_reset_preserve_audit(
    agent_store: SimpleNamespace,
) -> None:
    scope, current = await _scope_and_session()
    current.turn_count = 4
    current.last_turn_at = datetime.utcnow() - timedelta(hours=8)
    async with agent_store.session_factory() as db:
        row = await db.get(ContextSession, current.id)
        row.turn_count = current.turn_count
        row.last_turn_at = current.last_turn_at
        await db.commit()

    active, reason = await session_manager.ensure_active_session(
        scope,
        _baseline(),
        idle_minutes=60,
        max_turns=40,
    )
    assert active.id == current.id
    assert reason == "idle"

    continued = await session_manager.rollover_session(
        scope,
        active,
        _baseline(),
        reason="idle",
        handoff_json='{"topic":"continue"}',
    )
    assert continued.sequence == 2
    assert continued.previous_session_id == current.id
    assert json.loads(continued.handoff_json)["topic"] == "continue"

    await session_manager.seal_scope_sessions(scope.id)
    fresh = await session_manager.create_session(scope.id, _baseline("new-model"), start_reason="hard_reset")
    sessions = await session_manager.list_scope_sessions(scope.id)
    assert fresh.status == "active"
    assert [item.status for item in sessions] == ["active", "sealed", "sealed"]
    assert await session_manager.get_session_by_ref(current.session_ref) is not None


@pytest.mark.asyncio
async def test_history_query_enforces_scope_archive_payload_and_pin_authorization(
    agent_store: SimpleNamespace,
) -> None:
    scope, first = await _scope_and_session()
    turn = await session_manager.start_turn(
        first,
        trigger_message_id="message",
        user_id="alice",
        user_name="Alice",
        conversation_user_id=None,
        fresh_context=False,
    )
    recorder = AgentTurnRecorder()
    recorder.record_user_input(
        "source",
        user_name="Alice",
        fresh_context=False,
        attachments=[
            {
                "attachment_ref": "input_dddddddddddddddddddddddddddddddd",
                "mime": "image/png",
                "bytes": 68,
                "source": "direct",
                "index": 1,
            }
        ],
    )
    recorder.append("assistant_output", role="assistant", payload={"content": "large-source"})
    await agent_events.persist_agent_events(turn.id, recorder.events)
    await session_manager.finish_turn(turn.id, status="completed", final_text="large-source")
    event = (await agent_events.load_turn_events(turn.id))[1]
    user_event = (await agent_events.load_turn_events(turn.id))[0]
    second = await session_manager.rollover_session(scope, first, _baseline(), reason="manual_new", carry_handoff=False)

    denied = AgentAccessContext(scope.id, second.id, 999, "alice")
    with pytest.raises(agent_query.AgentQueryError, match="Archived session access"):
        await agent_query.list_sessions_payload(denied, limit=10)

    archive_access = AgentAccessContext(
        scope.id,
        second.id,
        999,
        "alice",
        allow_archived_sessions=True,
    )
    listed = await agent_query.list_sessions_payload(archive_access, limit=10)
    sessions = cast(list[dict[str, object]], listed["sessions"])
    assert [item["session_ref"] for item in sessions] == [second.session_ref, first.session_ref]
    with pytest.raises(agent_query.AgentQueryError, match="Stored payload access"):
        await agent_query.read_event_payload(
            archive_access,
            event_ref=event.event_ref,
            path="content",
            max_chars=1000,
        )

    full_access = AgentAccessContext(
        scope.id,
        second.id,
        999,
        "alice",
        allow_archived_sessions=True,
        allow_payload_delivery=True,
        allow_context_pin=True,
    )
    payload = await agent_query.read_event_payload(
        full_access,
        event_ref=event.event_ref,
        path="content",
        max_chars=1000,
    )
    assert payload["data"] == "large-source"
    with pytest.raises(agent_query.AgentQueryError, match="not model-readable"):
        await agent_query.read_event_payload(
            full_access,
            event_ref=user_event.event_ref,
            path="attachments",
            max_chars=1000,
        )
    pinned = await agent_query.pin_context_payload(
        full_access,
        event_ref=event.event_ref,
        label="Important source",
    )
    assert pinned["pinned"] is True
    assert (await session_manager.load_scope_anchors(scope.id))[0][1].event_ref == event.event_ref


@pytest.mark.asyncio
async def test_legacy_migration_is_one_time_and_preserves_tool_evidence(
    agent_store: SimpleNamespace,
) -> None:
    async with agent_store.session_factory() as db:
        user = Conversation(
            channel_id="legacy-channel",
            user_id="alice",
            user_name="Alice",
            role="user",
            content="search",
        )
        assistant = Conversation(
            channel_id="legacy-channel",
            user_id="",
            user_name="bot",
            role="assistant",
            content="done",
        )
        db.add_all([user, assistant])
        await db.flush()
        db.add(
            ToolExecution(
                channel_id="legacy-channel",
                turn_id=user.id,
                sequence=1,
                tool_name="web_search",
                status="succeeded",
                effect="observed",
                arguments_json='{"query":"test"}',
                outcome_json='{"results":[]}',
            )
        )
        await db.commit()

    assert await agent_migration.migrate_legacy_agent_events() == 2
    assert await agent_migration.migrate_legacy_agent_events() == 0
    async with agent_store.session_factory() as db:
        migration = await db.get(MigrationState, "legacy_agent_events_v1")
        scope = (await db.execute(select(ChatScope).where(ChatScope.platform == "legacy"))).scalar_one()
        context_session = (
            await db.execute(select(ContextSession).where(ContextSession.scope_id == scope.id))
        ).scalar_one()
        turns = list((await db.execute(select(AgentTurn).where(AgentTurn.session_id == context_session.id))).scalars())
        events = list(
            (
                await db.execute(
                    select(AgentEvent)
                    .join(AgentTurn, AgentTurn.id == AgentEvent.turn_id)
                    .where(AgentTurn.session_id == context_session.id)
                    .order_by(AgentEvent.sequence)
                )
            ).scalars()
        )
    assert migration is not None
    assert migration.status == "completed"
    assert len(turns) == 1
    assert [event.event_type for event in events] == [
        "user_input",
        "assistant_tool_call",
        "tool_result",
        "assistant_output",
    ]
    assert load_event_payload(events[1])["arguments"] == {"query": "test"}


@pytest.mark.asyncio
async def test_model_attempt_recorder_preserves_retry_status_metrics_and_attempt_numbers() -> None:
    recorder = AgentTurnRecorder()
    trace = ToolTraceRecorder()

    async def fail() -> object:
        raise RuntimeError("provider failed")

    with pytest.raises(RuntimeError, match="provider failed"):
        await generation._record_model_attempt(
            fail,
            recorder=recorder,
            tool_trace=trace,
            model_name="test-model",
        )

    async def succeed() -> object:
        return SimpleNamespace(content="done", metrics={"input_tokens": 12, "output_tokens": 4})

    result = await generation._record_model_attempt(
        succeed,
        recorder=recorder,
        tool_trace=trace,
        model_name="test-model",
    )

    assert getattr(result, "content") == "done"
    assert [event.status for event in recorder.events] == ["failed", "succeeded"]
    assert [event.attempt for event in recorder.events] == [1, 2]
    assert recorder.events[1].payload["metrics"] == {"input_tokens": 12, "output_tokens": 4}
    assert trace.attempt == 2


@pytest.mark.asyncio
async def test_handoff_failure_uses_structured_evidence_linked_fallback(
    agent_store: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _scope, context_session = await _scope_and_session()
    turn = await session_manager.start_turn(
        context_session,
        trigger_message_id="message",
        user_id="alice",
        user_name="Alice",
        conversation_user_id=None,
        fresh_context=False,
    )
    recorder = AgentTurnRecorder()
    recorder.record_user_input("Continue the report", user_name="Alice", fresh_context=False)
    recorder.record_assistant_output("The first draft is ready")
    await agent_events.persist_agent_events(turn.id, recorder.events)
    await session_manager.finish_turn(turn.id, status="completed", final_text="The first draft is ready")

    monkeypatch.setattr(session_handoff, "get_model_config", lambda *_args: SimpleNamespace(name="test"))

    async def fail_completion(**_kwargs: object) -> object:
        raise RuntimeError("handoff unavailable")

    monkeypatch.setattr(session_handoff.litellm, "acompletion", fail_completion)
    raw = await session_handoff.generate_session_handoff(
        context_session,
        model_name="test",
        channel_id="channel",
        timeout=1,
        source_max_chars=12000,
        output_max_chars=6000,
    )
    handoff = json.loads(raw)
    assert set(handoff) == {
        "topic",
        "goals",
        "decisions",
        "constraints",
        "open_loops",
        "confirmed_deliveries",
        "relevant_event_refs",
        "last_visible_exchange",
    }
    assert handoff["topic"] == "Continue the report"
    assert handoff["last_visible_exchange"] == {
        "user": "Continue the report",
        "assistant": "The first draft is ready",
    }
    actual_refs = {event.event_ref for event in await agent_events.load_turn_events(turn.id)}
    assert set(handoff["relevant_event_refs"]) <= actual_refs


def test_baseline_and_latest_user_authorization_intents_are_deterministic() -> None:
    first = context_builder.build_baseline_fingerprint(
        model_name="model",
        persona="persona",
        tool_schemas=[{"name": "html2pic", "source_hash": "one"}],
    )
    second = context_builder.build_baseline_fingerprint(
        model_name="model",
        persona="persona",
        tool_schemas=[{"name": "html2pic", "source_hash": "two"}],
    )
    assert first.tool_schema_hash != second.tool_schema_hash
    assert context_builder.requests_fresh_context("别管之前，重新回答") is True
    assert context_builder.requests_archived_context("上次会话里说了什么") is True
    assert context_builder.requests_tool_payload("继续修改刚才那个页面") is True
    assert context_builder.requests_context_pin("记住这个上下文，后面都按这个") is True
    assert context_builder.requests_tool_payload("随便聊聊") is False


@pytest.mark.asyncio
async def test_prepare_agent_turn_wires_session_context_and_persistence(
    agent_store: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    appended: list[tuple[object, ...]] = []

    async def relation(*_args: object) -> UserRelation:
        return UserRelation(
            user_id="alice",
            channel_id="channel",
            affection=30.0,
            trust=30.0,
            dependence=0.0,
            resentment=0.0,
            familiarity=0.0,
            impression="",
            eval_counter=0,
        )

    async def mood(*_args: object) -> float:
        return 0.25

    async def memory(*_args: object) -> MemoryContext:
        return MemoryContext(
            chat_profile={"preferences": ["偏好简洁回答"]},
            evaluator_profile_facts=[],
            relevant_memories=["上次部署回滚点在 /var/lib"],
            retrieval={
                "enabled": True,
                "query_embedded": True,
                "stored_profile_facts": 12,
                "stored_memories": 40,
                "profile_facts": [
                    {
                        "category": "preferences",
                        "key": "reply_style",
                        "value": "偏好简洁回答",
                        "confidence": 0.82,
                        "evidence_count": 4,
                        "similarity": 0.6123,
                    }
                ],
                "memories": [{"text": "上次部署回滚点在 /var/lib", "importance": 0.75, "similarity": 0.5412}],
                "thresholds": {"min_similarity": 0.35, "min_importance": 0.6},
            },
        )

    async def history(*_args: object) -> list[Conversation]:
        return []

    async def append(*args: object) -> int:
        appended.append(args)
        return 7

    async def delete(_message_id: int | None) -> None:
        return None

    monkeypatch.setattr(agent_turn_setup, "get_relation", relation)
    monkeypatch.setattr(agent_turn_setup, "get_mood", mood)
    monkeypatch.setattr(agent_turn_setup, "load_memory_context", memory)
    monkeypatch.setattr(agent_turn_setup, "load_history", history)
    monkeypatch.setattr(agent_turn_setup, "append_message", append)
    monkeypatch.setattr(agent_turn_setup, "delete_message", delete)

    fake_session = SimpleNamespace(
        account=SimpleNamespace(platform="test", self_id="bot"),
        channel=SimpleNamespace(id="channel", name="Test Channel"),
        guild=SimpleNamespace(id="guild", name="Test Guild"),
        event=SimpleNamespace(message=SimpleNamespace(id="message-1")),
    )
    prepared = await agent_turn_setup.prepare_agent_turn(
        LLMChatConfig(memory_enabled=False),
        cast(Session, fake_session),
        ChatIdentity(user_id="alice", display_name="Alice", participant_ref="participant_alice"),
        model_name="test-model",
        supports_image_input=False,
        model_text="hello",
        content="hello",
        current_content=None,
        forwarded_messages=(),
        mentioned_participants=(),
        input_attachments=[
            {
                "attachment_ref": "input_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "mime": "image/png",
                "bytes": 68,
                "source": "direct",
                "index": 1,
            }
        ],
        warn=lambda _message: None,
        tool_schemas=[{"name": "send_text", "source_hash": "test"}],
    )

    assert appended == [("channel", "alice", "Alice", "user", "hello")]
    assert prepared.mood == 0.25
    assert prepared.chat_messages[-1]["role"] == "user"
    assert "agent_session" in prepared.system
    assert prepared.agent_access.scope_id > 0
    assert prepared.lifecycle.agent_turn_id is not None

    async def stored_events() -> list[AgentEvent]:
        async with agent_store.session_factory() as db:
            return list(
                (
                    await db.execute(
                        select(AgentEvent)
                        .where(AgentEvent.turn_id == prepared.lifecycle.agent_turn_id)
                        .order_by(AgentEvent.sequence)
                    )
                ).scalars()
            )

    started = await stored_events()
    assert [event.event_type for event in started] == [
        "user_input",
        "persona_state",
        "engagement_decision",
        "context_selection",
    ]
    user_payload = load_event_payload(started[0])
    assert cast(list[dict[str, object]], user_payload["attachments"])[0]["attachment_ref"] == (
        "input_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    )
    user_view = serialize_event_view(started[0], user_payload)
    assert cast(list[dict[str, object]], user_view["images"])[0]["url"] == (
        "/api/llm-chat/sessions/events/" + started[0].event_ref + "/attachments/input_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    )

    assert [event.sequence for event in started] == [1, 2, 3, 4]

    persona_payload = load_event_payload(started[1])
    assert cast(dict[str, Any], persona_payload["relation"])["affection"] == 30.0
    assert cast(dict[str, Any], persona_payload["state"])["mood"] == 0.25
    assert cast(dict[str, Any], persona_payload["memory"])["stored_memories"] == 40
    assert persona_payload["prompt_memories"] == ["上次部署回滚点在 /var/lib"]

    persona_view = serialize_event_view(started[1], persona_payload)
    assert persona_view["title"] == "人格与记忆"
    persona = cast(dict[str, Any], persona_view["persona"])
    assert {"label": "好感", "value": "30"} in cast(list[dict[str, str]], persona["relation"])
    assert cast(list[dict[str, str]], persona["profile_facts"])[0]["scores"].startswith("相似度 0.6123")

    prepared.lifecycle.agent_events.record_assistant_output("好的")
    await prepared.lifecycle.finalize_agent_turn("completed")
    async with agent_store.session_factory() as db:
        turn = await db.get(AgentTurn, prepared.lifecycle.agent_turn_id)
    finished = await stored_events()
    assert turn is not None
    assert turn.status == "completed"
    assert [event.event_type for event in finished] == [
        "user_input",
        "persona_state",
        "engagement_decision",
        "context_selection",
        "assistant_output",
    ]
    assert [event.sequence for event in finished] == [1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_user_input_images_are_copied_to_private_audit_attachments(tmp_path: Path) -> None:
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
    )

    class _ImageSession:
        async def download(self, src: str) -> bytes:
            assert src in {"local://direct", "local://quoted"}
            return png

    attachments = await agent_attachments.capture_user_input_images(
        cast(Session, _ImageSession()),
        [
            (Image.of(url="local://direct"), False),
            (Image.of(url="local://quoted"), True),
        ],
        root=tmp_path,
    )

    assert [item["source"] for item in attachments] == ["direct", "quoted"]
    assert all("src" not in item and "path" not in item for item in attachments)
    for item in attachments:
        path = agent_attachments.resolve_user_input_attachment(
            cast(str, item["attachment_ref"]),
            cast(str, item["mime"]),
            root=tmp_path,
        )
        assert path.read_bytes() == png

    agent_attachments.remove_user_input_attachments(attachments, root=tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_cancelled_user_image_capture_removes_completed_files(tmp_path: Path) -> None:
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
    )
    blocker = asyncio.Event()

    class _CancelledSession:
        async def download(self, src: str) -> bytes:
            if src == "local://direct":
                return png
            await blocker.wait()
            return png

    task = asyncio.create_task(
        agent_attachments.capture_user_input_images(
            cast(Session, _CancelledSession()),
            [
                (Image.of(url="local://direct"), False),
                (Image.of(url="local://quoted"), True),
            ],
            root=tmp_path,
        )
    )
    for _ in range(100):
        if list(tmp_path.iterdir()):
            break
        await asyncio.sleep(0.01)
    assert list(tmp_path.iterdir())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_send_image_records_delivered_paths_as_auditable_evidence() -> None:
    trace = ToolTraceRecorder()
    call = trace.start("send_image", {"context": "开心", "image_paths": ["memes/12.jpg"]})
    tool_trace.record_tool_evidence({"images": [{"path": "memes/99.jpg", "meaning": "范围外", "text": ""}]})
    with tool_trace.llm_chat_tool_trace_scope(trace), tool_trace.llm_chat_tool_execution_scope(call.execution_ref):
        tool_trace.record_tool_evidence({"images": [{"path": "memes/12.jpg", "meaning": "开心大笑", "text": "哈哈"}]})
        tool_trace.record_tool_evidence({"images": [{"path": "memes/34.gif", "meaning": "撒娇", "text": ""}]})
    trace.finish_success(
        call,
        "已发送 2 张图片",
        before=DeliverySnapshot(active=True),
        after=DeliverySnapshot(active=True, attempts=2, confirmed=2, confirmed_media=2),
    )
    event = trace.events[0]
    assert event.recorded_arguments["image_paths"] == ["memes/12.jpg"]
    assert [image["path"] for image in cast(list[dict[str, str]], event.evidence["images"])] == [
        "memes/12.jpg",
        "memes/34.gif",
    ]

    recorder = AgentTurnRecorder()
    recorder.record_tool_events(trace.events)
    result_event = recorder.events[1]
    evidence = cast(dict[str, Any], result_event.payload["evidence"])
    assert [image["path"] for image in cast(list[dict[str, str]], evidence["images"])] == [
        "memes/12.jpg",
        "memes/34.gif",
    ]

    view = serialize_event_view(
        AgentEvent(
            event_ref="event_view",
            turn_id=1,
            sequence=2,
            event_type="tool_result",
            role="tool",
            tool_name="send_image",
            payload_json=json.dumps(result_event.payload, ensure_ascii=False),
            status="succeeded",
            effect="confirmed",
            duration_ms=120,
        ),
        result_event.payload,
    )
    images = cast(list[dict[str, str]], cast(dict[str, Any], view["evidence"])["images"])
    assert [image["url"] for image in images] == [
        "/api/llm-chat/memes/files/12.jpg",
        "/api/llm-chat/memes/files/34.gif",
    ]
    assert images[0]["meaning"] == "开心大笑"
    assert view["title"] == "send_image"
    assert view["preview"] == "已发送 2 张图片"
    assert {"label": "耗时", "value": "120 ms"} in cast(list[dict[str, str]], view["details"])


@pytest.mark.asyncio
async def test_background_tag_image_result_replaces_pending_event(agent_store: SimpleNamespace) -> None:
    _scope, context_session = await _scope_and_session()
    turn = await session_manager.start_turn(
        context_session,
        trigger_message_id="message",
        user_id="alice",
        user_name="Alice",
        conversation_user_id=None,
        fresh_context=False,
    )
    async with agent_store.session_factory() as db:
        db.add(
            AgentEvent(
                turn_id=turn.id,
                sequence=1,
                event_type="tool_result",
                role="tool",
                execution_ref="exec_tag",
                tool_call_id="exec_tag",
                tool_name="tag_image",
                payload_json=json.dumps(
                    {
                        "result": {"status": "pending", "summary": "processing"},
                        "context_result": {"status": "pending", "summary": "processing"},
                    }
                ),
                status="pending",
                effect="none",
            )
        )
        await db.commit()

    assert await agent_events.settle_background_tool_result(
        turn.id,
        "exec_tag",
        status="succeeded",
        effect="confirmed",
        result={"status": "created", "summary": "Collected in background."},
        duration_ms=65_000,
        wait_seconds=0,
    )

    event = (await agent_events.load_turn_events(turn.id))[0]
    assert event.status == "succeeded"
    assert event.effect == "confirmed"
    assert event.duration_ms == 65_000
    assert load_event_payload(event)["result"] == {
        "status": "created",
        "summary": "Collected in background.",
    }


@pytest.mark.asyncio
async def test_event_view_inlines_key_content_without_extra_read_step() -> None:
    payload = {
        "arguments": {"text": "先确认回滚点", "delay_seconds": 1.2},
        "context_arguments": {"text_chars": 6},
    }
    event = AgentEvent(
        event_ref="event_call",
        turn_id=1,
        sequence=1,
        event_type="assistant_tool_call",
        role="assistant",
        tool_name="send_text",
        payload_json=json.dumps(payload, ensure_ascii=False),
        status="requested",
        effect="none",
    )
    view = serialize_event_view(event, payload)
    assert view["preview"] == "先确认回滚点"
    assert view["arguments"] == payload["arguments"]
    assert view["evidence"] is None
    assert view["payload_chars"] == len(event.payload_json)
    assert {"label": "文本", "value": "先确认回滚点"} in cast(list[dict[str, str]], view["details"])

    user_payload = {
        "content": json.dumps(
            {"speaker": "FrostN0v0", "content": "帮我发个开心的表情"},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        "speaker": "FrostN0v0",
        "fresh_context": False,
    }
    user_event = AgentEvent(
        event_ref="event_user",
        turn_id=1,
        sequence=2,
        event_type="user_input",
        role="user",
        payload_json=json.dumps(user_payload, ensure_ascii=False),
    )
    user_view = serialize_event_view(user_event, user_payload)
    assert user_view["preview"] == "FrostN0v0：帮我发个开心的表情"
    assert "{" not in cast(str, user_view["preview"])
    assert user_view["title"] == "用户输入"

    untitled = AgentEvent(
        event_ref="event_unknown",
        turn_id=1,
        sequence=3,
        event_type="future_event",
        role="",
        payload_json="{}",
    )
    assert serialize_event_view(untitled, {})["title"] == "future_event"


@pytest.mark.asyncio
async def test_scope_identity_resolves_channel_name_and_backs_off_on_failure(
    agent_store: SimpleNamespace,
) -> None:
    lookups: list[str] = []

    class _Account:
        platform = "qq"
        self_id = "bot"

    class _Session:
        channel = SimpleNamespace(id="957729172", name="")
        account = _Account()
        guild = SimpleNamespace(id="957729172", name="")

        async def channel_get(self, channel_id: str) -> SimpleNamespace:
            lookups.append(channel_id)
            return SimpleNamespace(name="珂朵莉的测试群")

        async def guild_get(self, guild_id: str) -> SimpleNamespace:
            return SimpleNamespace(name="")

    session = cast(Session, _Session())
    identity = await session_manager.resolve_scope_identity(session)
    assert identity.display_name == "珂朵莉的测试群"
    assert lookups == ["957729172"]

    cached = await session_manager.resolve_scope_identity(session)
    assert cached.display_name == "珂朵莉的测试群"
    assert lookups == ["957729172"]

    scope = await session_manager.get_or_create_scope(identity)
    assert scope.display_name == "珂朵莉的测试群"

    class _FailingSession(_Session):
        channel = SimpleNamespace(id="1032658892", name="")
        guild = SimpleNamespace(id="", name="")

        async def channel_get(self, channel_id: str) -> SimpleNamespace:
            lookups.append(channel_id)
            raise RuntimeError("channel lookup unavailable")

    failing = cast(Session, _FailingSession())
    assert (await session_manager.resolve_scope_identity(failing)).display_name == ""
    assert lookups == ["957729172", "1032658892"]

    assert (await session_manager.resolve_scope_identity(failing)).display_name == ""
    assert lookups == ["957729172", "1032658892"]

    retried = await session_manager.resolve_scope_identity(
        failing,
        now=datetime.utcnow() + timedelta(minutes=31),
    )
    assert retried.display_name == ""
    assert lookups == ["957729172", "1032658892", "1032658892"]

    unnamed = await session_manager.get_or_create_scope(
        ScopeIdentity(
            platform="qq",
            account_id="bot",
            guild_id="",
            channel_id="1032658892",
            display_name="",
        )
    )
    assert unnamed.display_name == ""

    control_session = _Session()
    control_session.channel = SimpleNamespace(id="1041124011", name="Asgard-RUST腐蚀\u0011\u0010\u007f  腐蚀\t侧")
    control_session.guild = SimpleNamespace(id="", name="")
    sanitized = await session_manager.resolve_scope_identity(cast(Session, control_session))
    assert sanitized.display_name == "Asgard-RUST腐蚀 腐蚀 侧"

    legacy = await session_manager.get_or_create_scope(
        ScopeIdentity(
            platform="legacy",
            account_id="",
            guild_id="",
            channel_id="326466216",
            display_name="326466216",
        )
    )
    assert AgentAdminService._channel_name(legacy) == ""
    assert session_manager.clean_channel_name("  群\u0007名  ") == "群名"
    assert session_manager.clean_channel_name(None) == ""


@pytest.mark.asyncio
async def test_flushed_event_sequences_stay_frozen_against_earlier_tool_timestamps() -> None:
    recorder = AgentTurnRecorder()
    recorder.record_user_input("hi", user_name="Alice", fresh_context=False)
    recorder.record_persona_state({"relation": {"affection": 30.0}})
    started = recorder.pending_events()
    assert [event.sequence for event in started] == [1, 2]

    recorder.mark_flushed(len(started))
    assert recorder.pending_events() == ()

    trace = ToolTraceRecorder()
    call = trace.start("send_text", {"text": "稍等"})
    trace.finish_success(
        call,
        "已发送 1 条文本消息",
        before=DeliverySnapshot(active=True),
        after=DeliverySnapshot(active=True, attempts=1, confirmed=1),
    )
    stale = replace(trace.events[0], started_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
    recorder.record_tool_events([stale])

    assert [event.sequence for event in recorder.events[:2]] == [1, 2]
    assert [event.event_type for event in recorder.events[:2]] == ["user_input", "persona_state"]
    pending = recorder.pending_events()
    assert [event.event_type for event in pending] == ["assistant_tool_call", "tool_result"]
    assert [event.sequence for event in pending] == [3, 4]
    assert all(event.created_at >= recorder.events[1].created_at for event in pending)
