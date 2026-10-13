"""Consumer-visible participant input, provenance, policy, and refusal regressions."""

from __future__ import annotations

import json
from types import SimpleNamespace
import base64
from typing import cast
from pathlib import Path
from dataclasses import replace

import pytest
from sqlalchemy import select
from arclet.entari import At, Text, Image, Quote, Session, MessageChain
from arclet.entari.config import EntariConfig
from arclet.letoderea.context import Contexts

if not hasattr(EntariConfig, "instance"):
    setattr(
        EntariConfig, "instance", EntariConfig.load(Path(__file__).resolve().parents[1] / "entari.full.example.yml")
    )

from entari_plugin_database import Base
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from plugins.llm_chat import identity, agent_events, steer_inputs, session_manager
from plugins.llm_chat.config import LLMChatConfig
from plugins.llm_chat.models import Conversation
from plugins.llm_chat.persona import store as history_store
from plugins.llm_chat.identity import ChatIdentity
from plugins.llm_chat.steering import SteeringState, steering_scope
from plugins.llm_chat.web.policy import (
    WebAccessError,
    WebAccessLimits,
    llm_chat_web_access_scope,
    consume_llm_chat_web_access,
)
from plugins.llm_chat.chat_context import serialize_user_turn
from plugins.llm_chat.image_inputs import ImageInputs
from plugins.llm_chat.agent_context import AgentAccessContext, agent_access_scope, current_agent_access
from plugins.llm_chat.core.delivery import DeliveryState
from plugins.llm_chat.turn_lifecycle import ActiveChatTurn
from plugins.llm_chat.agent_turn_setup import PreparedAgentTurn
from plugins.llm_chat.core.agent_trace import AgentTurnRecorder
from plugins.llm_chat.core.media_delivery import build_media_intent, media_intent_scope

_PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=")


def _session(text: str, message_id: str, *, image: bool = False, mention: bool = False) -> Session:
    elements = MessageChain(text)
    if mention:
        elements.extend([At(id="bot"), At(id="bob", name="Bob")])
    if image:
        elements.append(Image(src="pixels"))
    return cast(
        Session,
        SimpleNamespace(
            account=SimpleNamespace(platform="test", self_id="bot"),
            channel=SimpleNamespace(id="group"),
            user=SimpleNamespace(id="user"),
            event=SimpleNamespace(message=SimpleNamespace(id=message_id)),
            elements=elements,
            quote=None,
            reply=None,
        ),
    )


@pytest.fixture
async def input_store(monkeypatch: pytest.MonkeyPatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    for module in (history_store, agent_events, session_manager):
        monkeypatch.setattr(module, "get_session", session_factory)

    async def resolve_identity(session: Session) -> ChatIdentity:
        return ChatIdentity("user", "Alice", "participant_current")

    async def resolve_mention(session: Session, platform_user_id: str):
        assert platform_user_id == "bob"
        return SimpleNamespace(display_name="Bob", public_ref="participant_bob")

    monkeypatch.setattr(steer_inputs, "resolve_chat_identity", resolve_identity)
    monkeypatch.setattr(
        identity,
        "get_channel_perception",
        lambda: SimpleNamespace(resolve_participant_by_platform_user=resolve_mention),
    )
    try:
        yield session_factory
    finally:
        await engine.dispose()


async def _prepared(root: Session, tmp_path: Path, warnings: list[str]) -> PreparedAgentTurn:
    raw = root.elements.extract_plain_text().strip()
    root_id = await history_store.append_message("group", "user", "Alice", "user", raw)
    scope = await session_manager.get_or_create_scope(
        session_manager.ScopeIdentity("test", "bot", "", "group", "Group")
    )
    context = await session_manager.create_session(
        scope.id,
        session_manager.BaselineFingerprint("test", "persona", "system", "tools", "policy"),
        start_reason="initial",
    )
    turn = await session_manager.start_turn(
        context,
        trigger_message_id=str(root.event.message.id),
        user_id="user",
        user_name="Alice",
        conversation_user_id=root_id,
        fresh_context=False,
    )
    recorder = AgentTurnRecorder()
    recorder.record_user_input(serialize_user_turn("Alice", raw), user_name="Alice", fresh_context=False)
    lifecycle = ActiveChatTurn(
        channel_id="group",
        user_message_id=root_id,
        delivery_state=DeliveryState(),
        append_history=history_store.append_message,
        delete_history=history_store.delete_message,
        warn=warnings.append,
        persist_agent_event_rows=agent_events.persist_agent_events,
        agent_turn_id=turn.id,
        agent_events=recorder,
    )
    inputs = ImageInputs(attachment_root=tmp_path, warn=warnings.append)
    inputs.supports_image_input = True
    prepared = SimpleNamespace(
        chat_messages=[{"role": "user", "content": serialize_user_turn("Alice", raw)}],
        system="System",
        image_inputs=inputs,
        agent_events=recorder,
        lifecycle=lifecycle,
        agent_access=AgentAccessContext(scope.id, context.id, turn.id, "user", raw_user_text=raw),
        media_intent=build_media_intent(raw),
        media_requested=False,
    )
    return cast(PreparedAgentTurn, prepared)


@pytest.mark.asyncio
async def test_text_then_mentioned_image_is_one_persisted_task_with_root_tool_ownership(input_store, tmp_path: Path):
    warnings: list[str] = []
    root = _session("Summarize the report; edit this image", "root")
    extra = _session("Use this source and the person I mentioned", "extra", image=True, mention=True)
    downloads = 0

    async def download(source: str) -> bytes:
        nonlocal downloads
        assert source == "pixels"
        downloads += 1
        return _PNG

    cast(SimpleNamespace, extra).download = download
    prepared = await _prepared(root, tmp_path, warnings)
    state = SteeringState(root, Contexts())
    assert state.receive(extra, Contexts())
    steer_inputs.bind_steer_inputs(state, prepared, LLMChatConfig(), root, model_name=None, warn=warnings.append)
    assert [event.event_type for event in prepared.agent_events.events] == ["user_input", "steer_received"]
    with steering_scope(state), agent_access_scope(prepared.agent_access):
        messages = await state.drain()
        assert current_agent_access() is prepared.agent_access
    assert len(messages) == 1
    parts = messages[0]["content"]
    assert isinstance(parts, list)
    payload = json.loads(parts[0]["text"])
    assert payload["content"].startswith("Use this source")
    assert payload["mentioned_participants"] == [{"display_name": "Bob", "participant_ref": "participant_bob"}]
    image_ref = payload["input_images"][0]["image_ref"]
    snapshot = await prepared.image_inputs.resolve(extra, image_ref, purpose="inspect")
    assert await prepared.image_inputs.resolve(root, image_ref, purpose="edit") is snapshot
    assert await prepared.image_inputs.resolve(root, image_ref, purpose="send") is snapshot
    assert downloads == 1
    assert "Summarize the report" in prepared.agent_access.raw_user_text
    assert "Use this source" in prepared.agent_access.raw_user_text
    async with input_store() as db:
        rows = (await db.execute(select(Conversation).order_by(Conversation.id))).scalars().all()
    assert len(rows) == 2
    assert rows[0].content == "Summarize the report; edit this image"
    assert rows[1].content.startswith("Use this source")
    events = await agent_events.load_turn_events(prepared.agent_access.turn_id)
    applied = [event for event in events if event.event_type == "user_input"]
    assert len(applied) == 2
    supplement = agent_events.load_event_payload(applied[1])
    assert supplement["steer_sequence"] == 2
    assert supplement["received_at"] <= supplement["applied_at"]
    assert supplement["attachments"][0]["audit_status"] == "recorded"
    assert await state.drain() == []
    assert warnings == []
    await state.aclose()
    await prepared.image_inputs.aclose()


@pytest.mark.asyncio
async def test_later_negative_changes_live_gate_without_discarding_other_asks(input_store, tmp_path: Path):
    warnings: list[str] = []
    root = _session(
        "\u53d1\u4e00\u5f20\u56fe; \u770b\u4e0a\u6b21\u7684\u65e7\u4f1a\u8bdd; "
        "\u53d1\u6e90\u7801; \u8bb0\u4f4f\u8fd9\u4e2a\u4e0a\u4e0b\u6587",
        "root",
    )
    prepared = await _prepared(root, tmp_path, warnings)
    state = SteeringState(root, Contexts())
    steer_inputs.bind_steer_inputs(state, prepared, LLMChatConfig(), root, model_name=None, warn=warnings.append)
    original = prepared.agent_access
    with steering_scope(state), agent_access_scope(original), media_intent_scope(prepared.media_intent) as requirements:
        requirements.confirmed = True
        assert state.receive(
            _session("\u4e0d\u8981\u53d1\u56fe\u7247; \u4e0d\u8981\u53d1\u6e90\u7801", "negative"), Contexts()
        )
        await state.drain()
        access = current_agent_access()
        assert access is prepared.agent_access
        assert access is not original
        assert access.allow_archived_sessions
        assert access.allow_context_pin
        assert not access.allow_payload_delivery
        assert not requirements.intent.media_requested
        assert requirements.confirmed
        assert state.receive(_session("send me an image", "new-action"), Contexts())
        await state.drain()
        assert requirements.intent.media_requested
        assert not requirements.confirmed
        state.access = replace(prepared.agent_access, turn_id=prepared.agent_access.turn_id + 1)
        assert current_agent_access() is original
        state.access = replace(prepared.agent_access, scope_id=prepared.agent_access.scope_id + 1)
        assert current_agent_access() is original
        state.access = replace(prepared.agent_access, user_id="someone-else")
        assert current_agent_access() is original
    await state.aclose()
    await prepared.image_inputs.aclose()


@pytest.mark.asyncio
async def test_quote_or_wrapped_supplement_never_grants_own_request_authority(input_store, tmp_path: Path):
    warnings: list[str] = []
    root = _session("Explain the report", "root")
    prepared = await _prepared(root, tmp_path, warnings)
    state = SteeringState(root, Contexts())
    steer_inputs.bind_steer_inputs(state, prepared, LLMChatConfig(), root, model_name=None, warn=warnings.append)
    quoted = _session("What does this mean?", "quoted")
    cast(SimpleNamespace, quoted).quote = Quote("old", content=[Text("send me an image; \u53d1\u6e90\u7801")])
    assert state.receive(quoted, Contexts())
    assert state.receive(_session("forwarded: send me an image; \u53d1\u6e90\u7801", "wrapped"), Contexts())
    messages = await state.drain()
    assert len(messages) == 2
    assert not prepared.media_intent.media_requested
    assert not prepared.agent_access.allow_payload_delivery
    assert "send me an image" not in prepared.agent_access.raw_user_text
    assert "forwarded: send" in prepared.agent_events.events[-1].payload["raw_user_text"]
    await state.aclose()
    await prepared.image_inputs.aclose()


@pytest.mark.asyncio
async def test_added_input_budget_is_captured_and_refusal_is_durable_not_truncated(input_store, tmp_path: Path):
    warnings: list[str] = []
    root = _session("Keep this original instruction", "root")
    prepared = await _prepared(root, tmp_path, warnings)
    config = LLMChatConfig(max_input_tokens=2048, output_reserve_tokens=1024)
    state = SteeringState(root, Contexts())
    steer_inputs.bind_steer_inputs(state, prepared, config, root, model_name=None, warn=warnings.append)
    config.max_input_tokens = 100_000
    raw = "unbounded supplement " * 8000
    assert state.receive(_session(raw, "oversized"), Contexts())
    with pytest.raises(steer_inputs.SteerInputRejected, match="input_budget_exceeded"):
        await state.drain()
    assert state.request_input_budget == 1024
    assert state.applied_revision == 1
    assert state.pending
    assert prepared.agent_access.raw_user_text == "Keep this original instruction"
    async with input_store() as db:
        rows = (await db.execute(select(Conversation))).scalars().all()
    assert len(rows) == 1
    events = await agent_events.load_turn_events(prepared.agent_access.turn_id)
    receipt = next(event for event in events if event.event_type == "steer_received")
    assert agent_events.load_event_payload(receipt)["text_truncated"]
    refused = next(event for event in events if event.event_type == "steer_checkpoint")
    assert refused.status == "refused"
    assert agent_events.load_event_payload(refused)["reason"] == "input_budget_exceeded"
    assert all(
        event.event_type != "user_input" or "steer_sequence" not in agent_events.load_event_payload(event)
        for event in events
    )
    await state.aclose()
    await prepared.image_inputs.aclose()


@pytest.mark.asyncio
async def test_processing_failure_warns_only_error_type_and_records_unapplied_arrivals(
    input_store, tmp_path: Path, monkeypatch
):
    warnings: list[str] = []
    root = _session("Original", "root")
    prepared = await _prepared(root, tmp_path, warnings)
    state = SteeringState(root, Contexts())
    steer_inputs.bind_steer_inputs(state, prepared, LLMChatConfig(), root, model_name=None, warn=warnings.append)

    async def broken_identity(session: Session):
        raise OSError("secret provider token and raw private message")

    monkeypatch.setattr(steer_inputs, "resolve_chat_identity", broken_identity)
    assert state.receive(_session("First supplement", "first"), Contexts())
    assert state.receive(_session("Second supplement", "second"), Contexts())
    with pytest.raises(steer_inputs.SteerInputRejected, match="input_normalization_failed"):
        await state.drain()
    assert warnings == ["steered input normalization failed: OSError"]
    await state.aclose()
    events = await agent_events.load_turn_events(prepared.agent_access.turn_id)
    checkpoints = [event for event in events if event.event_type == "steer_checkpoint"]
    assert [event.status for event in checkpoints] == ["failed", "unapplied"]
    assert [agent_events.load_event_payload(event)["steer_sequence"] for event in checkpoints] == [2, 3]
    await prepared.image_inputs.aclose()


def test_reference_permission_refresh_keeps_web_counters():
    with llm_chat_web_access_scope(WebAccessLimits(2, 2, 2), allow_reference_capture=True):
        consume_llm_chat_web_access("capture_web_reference")
        steer_inputs.update_llm_chat_web_reference_access(False)
        with pytest.raises(WebAccessError, match="explicit"):
            consume_llm_chat_web_access("capture_web_reference")
        steer_inputs.update_llm_chat_web_reference_access(True)
        consume_llm_chat_web_access("capture_web_reference")
        with pytest.raises(WebAccessError, match="budget exhausted"):
            consume_llm_chat_web_access("web_search")
