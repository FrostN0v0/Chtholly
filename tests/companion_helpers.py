"""Native companion test seams: isolated SQLite and external provider/transport only."""

from __future__ import annotations

import json
from uuid import uuid4
from types import SimpleNamespace
import asyncio
from pathlib import Path
from datetime import datetime, timezone, timedelta
import importlib
from contextlib import asynccontextmanager
from collections import deque

import pytest
from satori import Role, User, Event, Guild, Login, Member, Channel, EventType, ChannelType, MessageObject
import litellm
from sqlalchemy import select
from satori.const import Api
from arclet.entari import Session, MessageCreatedEvent
from satori.client import Account, ApiInfo
import pytest_asyncio
from arclet.letoderea import Scope
from arclet.entari.config import EntariConfig
from arclet.entari.plugin import niche
from arclet.entari.session import EntariProtocol
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from arclet.entari.event.api import SendResponse
from arclet.entari.plugin.model import PluginDispatcher, current_plugin

if not hasattr(EntariConfig, "instance"):
    EntariConfig.instance = EntariConfig.load(Path(__file__).resolve().parents[1] / "entari.full.example.yml")

from entari_plugin_database import Base
from entari_plugin_llm.event import LLMToolEvent
from entari_plugin_llm.tools import available_functions

from plugins.llm_chat import personality, agent_events, context_builder, session_manager
from plugins.llm_chat.config import LLMChatConfig, PersonaConfig
from plugins.llm_chat.models import AgentTurn, ScopePersonaSelection
from plugins.llm_chat.persona import store as persona_store, memory_context
from plugins.llm_chat.companion import store, service, observations
from plugins.llm_chat.relationships import state as relationship_state, evidence
from plugins.llm_chat.delivery_audit import install_delivery_audit
from plugins.llm_chat.group_delivery import install_group_delivery
from plugins.llm_chat.session_manager import ScopeIdentity

UTC = timezone.utc
NOW = datetime(2026, 10, 10, 2, 0, tzinfo=UTC)
MODEL = SimpleNamespace(
    name="test-model",
    alias="test-model",
    prompt="",
    extra={},
    base_url="https://provider.invalid/v1",
    api_key="not-a-real-key",
)


class Clock:
    def __init__(self, now=NOW):
        self.now = now
        self.monotonic = 0.0

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)

    async def sleep(self, seconds):
        self.monotonic += seconds
        await asyncio.sleep(0)


def config(**overrides):
    values = {
        "model": "test-model",
        "memory_enabled": False,
        "default_persona": "alpha",
        "personas": {
            "alpha": PersonaConfig(name="Alpha", prompt="Alpha is a quiet fictional painter."),
            "beta": PersonaConfig(name="Beta", prompt="Beta is an independent fictional reader."),
        },
    }
    values.update(overrides)
    return LLMChatConfig(**values)


@pytest_asyncio.fixture
async def companion_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'native-companion.db'}", connect_args={"timeout": 15}
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    for module in (
        store,
        service,
        observations,
        agent_events,
        context_builder,
        personality,
        session_manager,
        persona_store,
        memory_context,
        relationship_state,
        evidence,
    ):
        monkeypatch.setattr(module, "get_session", factory)
    await store.initialize_store(now=NOW)
    try:
        yield SimpleNamespace(engine=engine, factory=factory)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def native_transport(monkeypatch):
    clock = Clock()
    account = Account(
        Login(sn=0, platform="onebot", adapter="onebot", user=User(id="companion-bot", name="Bot")),
        ApiInfo(),
        [],
        EntariProtocol,
    )
    account.connected.set()
    network = SimpleNamespace(
        wire=[],
        receipts=[],
        lookups=[],
        hold="",
        entered=asyncio.Event(),
        release=asyncio.Event(),
        outcome="success",
    )

    async def call_api(action, params):
        if action == Api.CHANNEL_GET:
            network.lookups.append(params["channel_id"])
            channel_type = ChannelType.DIRECT if params["channel_id"].startswith("private:") else ChannelType.TEXT
            return {"id": params["channel_id"], "name": "Companion test channel", "type": channel_type.value}
        if action == Api.MESSAGE_CREATE:
            network.wire.append(dict(params))
            if network.hold and network.hold in params["content"]:
                network.entered.set()
                await network.release.wait()
            if network.outcome == "unknown":
                raise TimeoutError("The external message result is unknown")
            if network.outcome == "empty":
                return []
            return [{"id": f"native-receipt-{len(network.wire)}", "content": params["content"]}]
        raise AssertionError(f"Unexpected external API action: {action}")

    monkeypatch.setattr(account.protocol, "call_api", call_api)
    group_dispose = install_group_delivery(clock=lambda: clock.monotonic, sleep=clock.sleep)
    audit_dispose = install_delivery_audit()
    receipt_scope = Scope.of()

    async def observe_receipts(event: SendResponse):
        if event.account is account:
            network.receipts.extend(event.result)

    receipt_scope.register(observe_receipts, event=SendResponse)

    def session(
        *,
        channel="companion-group",
        user="platform-alice",
        message="inbound-real",
        content="A real question",
        channel_type=ChannelType.TEXT,
        roles=(),
    ):
        speaker = User(id=user, name="Alice" if user == "platform-alice" else "Bob")
        origin = Event(
            EventType.MESSAGE_CREATED,
            NOW,
            account.self_info,
            channel=Channel(id=channel, type=channel_type, name="Companion test channel"),
            guild=Guild(id=channel, name="Companion test channel") if channel_type == ChannelType.TEXT else None,
            member=Member(user=speaker, roles=[Role(id=role) for role in roles])
            if channel_type == ChannelType.TEXT
            else None,
            user=speaker,
            message=MessageObject(id=message, content=content),
        )
        return Session(account, MessageCreatedEvent(account, origin))

    try:
        yield SimpleNamespace(account=account, network=network, session=session, clock=clock)
    finally:
        network.release.set()
        await audit_dispose()
        await group_dispose()
        pending = receipt_scope.dispose()
        if pending:
            await asyncio.gather(*pending)
        await account.protocol.session.close()


def install_provider(monkeypatch: pytest.MonkeyPatch, responses):
    """Replace only the external completion and model-configuration boundary."""
    script = deque(responses)
    payloads = []

    async def completion(**payload):
        payloads.append(payload)
        if not script:
            raise AssertionError("Unexpected extra external completion request")
        result = script.popleft()
        if isinstance(result, BaseException):
            raise result
        if callable(result):
            result = result(payload)
            if hasattr(result, "__await__"):
                result = await result
        return result

    monkeypatch.setattr(litellm, "acompletion", completion)
    for module_name in (
        "plugins.llm_chat.companion.provider",
        "plugins.llm_chat.companion.contact",
        "entari_plugin_llm.service",
    ):
        monkeypatch.setattr(importlib.import_module(module_name), "get_model_config", lambda *_args, **_kwargs: MODEL)
    return payloads


def response(content=None, *, calls=()):
    return litellm.ModelResponse(
        model=MODEL.name,
        choices=[
            {
                "index": 0,
                "finish_reason": "tool_calls" if calls else "stop",
                "message": {"role": "assistant", "content": content, "tool_calls": list(calls) or None},
            }
        ],
        usage={"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
    )


def tool_call(name, arguments, call_id):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


@asynccontextmanager
async def native_tools():
    """Use Entari's native niche lifecycle, with no import/package aliases."""
    owner = niche(f"companion-native-tests-{uuid4().hex}", config={})(lambda _plugin: None)
    owner.exec_apply()
    token = current_plugin.set(owner)
    before = dict(available_functions)
    try:
        from plugins.llm_chat.agno_compat import register_llm_chat_tool, install_agno_tool_bridge

        install_agno_tool_bridge()
        dispatcher = PluginDispatcher(owner, LLMToolEvent)
        dispatcher.register_hooks.append(register_llm_chat_tool)
        yield dispatcher
    finally:
        current_plugin.reset(token)
        pending = owner.dispose()
        if pending:
            assert all(task.get_loop() is asyncio.get_running_loop() for task in pending)
            await asyncio.gather(*pending)
        assert dict(available_functions) == before


async def scope(database, *, channel="companion-group", persona="alpha"):
    result = await session_manager.get_or_create_scope(
        ScopeIdentity("onebot", "companion-bot", channel, channel, "Companion test channel"),
    )
    async with database.factory() as db, db.begin():
        selection = await db.get(ScopePersonaSelection, result.id)
        if selection is None:
            db.add(ScopePersonaSelection(scope_id=result.id, persona_key=persona))
        else:
            selection.persona_key = persona
    return result


async def completed_turn(
    database, native_scope, *, user="alice-unified", text="I am waiting for my exam result", at=NOW
):
    from plugins.llm_chat.companion.context import mode_baseline

    persona = await personality.resolve_scope_persona(config(), native_scope.id)
    mode = await store.get_mode(native_scope.id)
    baseline = mode_baseline(
        context_builder.build_baseline_fingerprint(
            model_name=MODEL.name, persona=persona.baseline_text, tool_schemas=()
        ),
        mode.mode,
    )
    active = await session_manager.get_active_session(native_scope.id)
    if active is None:
        active = await session_manager.create_session(native_scope.id, baseline, start_reason="initial")
    turn = await session_manager.start_turn(
        active,
        trigger_message_id=f"real-message-{uuid4().hex}",
        user_id=user,
        user_name=user,
        conversation_user_id=None,
        fresh_context=False,
    )
    from plugins.llm_chat.core.agent_trace import AgentTurnRecorder

    recorder = AgentTurnRecorder()
    recorder.record_user_input(json.dumps({"speaker": user, "content": text}), user_name=user, fresh_context=False)
    recorder.append("assistant_output", role="assistant", payload={"content": "I understand."}, model_visible=True)
    await agent_events.persist_agent_events(turn.id, recorder.events)
    await session_manager.finish_turn(turn.id, status="completed", final_text="I understand.")
    async with database.factory() as db, db.begin():
        row = await db.get(AgentTurn, turn.id)
        row.created_at = (at - timedelta(seconds=1)).replace(tzinfo=None)
        row.finished_at = at.replace(tzinfo=None)
    return turn


async def enable_member(
    database, native_scope, *, user="alice-unified", platform_user="platform-alice", at=NOW - timedelta(minutes=10)
):
    await store.set_mode(native_scope.id, "companion", persona_key="alpha", now=at)
    turn = await completed_turn(database, native_scope, user=user, at=at)
    member = await store.record_chat(
        native_scope.id,
        "alpha",
        user_id=user,
        platform_user_id=platform_user,
        display_name="Alice" if platform_user == "platform-alice" else "Bob",
        turn_id=turn.id,
        now=at,
    )
    assert member is not None
    return member


async def settled(worker):
    await worker.claim_ready()
    await asyncio.wait_for(worker.drain(), 5)
    await asyncio.sleep(0)


async def until(predicate, *, timeout=5):
    async def await_predicate():
        while not predicate():
            await asyncio.sleep(0.001)

    await asyncio.wait_for(await_predicate(), timeout)


async def native_rows(database, model):
    async with database.factory() as db:
        return list((await db.scalars(select(model))).all())
