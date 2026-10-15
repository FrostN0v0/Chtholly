"""Direct-superuser native mode command permissions and scope-persistent state."""

from __future__ import annotations

from uuid import uuid4
from types import ModuleType, SimpleNamespace
import asyncio
from pathlib import Path
from datetime import datetime, timezone
from contextlib import asynccontextmanager

import pytest
from satori import User, Event, Login, EventType, ChannelType, MessageObject
from arclet.entari import Session
from satori.client import Account, ApiInfo
import pytest_asyncio
from arclet.letoderea import Contexts
from companion_helpers import (
    NOW,
    scope,
    config,
    response,
    tool_call,
    native_rows,
    native_tools,
    install_provider,
)
from entari_plugin_user import utils as user_utils
from arclet.entari.const import ITEM_SESSION
from arclet.entari.config import EntariConfig
from arclet.entari.plugin import find_plugin, get_plugins
from arclet.entari.command import execute
from arclet.entari.session import EntariProtocol
from entari_plugin_database import Base
from entari_plugin_user.models import Bind, User as UnifiedUser
from arclet.entari.plugin.service import plugin_service

from plugins.llm_chat import agent_turn_setup
from plugins.llm_chat.models import (
    AgentTurn,
    ChatScope,
    AgentEvent,
    Conversation,
    ReminderTask,
    UserRelation,
    ScopePersonaSelection,
)
from plugins.llm_chat.identity import ChatIdentity
from plugins.llm_chat.companion import store
from plugins.llm_chat.generation import generate_chat_response
from plugins.llm_chat.image_inputs import ImageInputs
from plugins.llm_chat.agent_context import agent_access_scope
from plugins.llm_chat.tools.send_msg import SendMsgToolContext, register_send_msg
from plugins.llm_chat.companion.models import CompanionMode, CompanionMember, CompanionReview, CompanionChatEvidence
from plugins.llm_chat.companion.runtime import install_companion_runtime
from plugins.llm_chat.companion.service import CompanionService
from plugins.llm_chat.tools.finish_turn import register_finish_turn

pytest_plugins = ["companion_helpers"]

NORMAL = "\u666e\u901a\u5bf9\u8bdd"
COMPANION = "\u81ea\u4e3b\u966a\u4f34"


@asynccontextmanager
async def native_mode_command(environment):
    """Execute the command source under Entari's native disposable owner."""
    name = "plugins.llm_chat.companion.command"
    source_path = Path(__file__).resolve().parents[1] / "plugins" / "llm_chat" / "companion" / "command.py"
    source = source_path.read_text(encoding="utf-8")

    def apply(owner):
        module = ModuleType(name)
        module.__file__ = str(source_path)
        module.__package__ = name.rpartition(".")[0]
        module.__plugin__ = owner
        owner.module = module
        exec(compile(source, str(source_path), "exec"), module.__dict__)

    owner = plugin_service.root.niche(f"companion-mode-tests-{uuid4().hex}", config={})(apply)
    owner.exec_apply()
    try:
        environment.configure(owner.module)
        yield SimpleNamespace(call=environment.call, module=owner.module, resolved=environment.resolved)
    finally:
        pending = owner.dispose()
        if pending:
            assert all(task.get_loop() is asyncio.get_running_loop() for task in pending)
            await asyncio.gather(*pending)


@pytest_asyncio.fixture
async def command_environment(companion_db, native_transport, monkeypatch):
    """Keep real User/Bind verification; isolate unrelated global command listeners."""
    async with companion_db.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(user_utils, "get_session", companion_db.factory)
    monkeypatch.setattr(EntariConfig.instance.basic, "superusers", {"onebot": ["platform-alice"]})
    user_plugin = find_plugin("entari_plugin_user")
    assert user_plugin is not None
    plugin_state = [(plugin, plugin.is_available, dict(plugin.config)) for plugin in get_plugins(subplugged=True)]
    resolved = []

    async def native_identity(session):
        # Only the external participant-resolution boundary is substituted. The
        # real Session and native User/Bind store determine the verified actor.
        assert isinstance(session, Session)
        assert session.account is native_transport.account
        assert session.account.platform == "onebot"
        assert session.user.id
        user = await user_utils.get_user(session.account.platform, session.user)
        resolved.append((session.channel.id, session.user.id, user.id))
        return ChatIdentity(str(user.id), session.user.name or "member", "")

    def configure(module):
        monkeypatch.setattr(module, "get_session", companion_db.factory)
        monkeypatch.setattr(module, "resolve_chat_identity", native_identity)

    async def callback(session, mode=""):
        from arclet.entari import MessageCreatedEvent

        content = "llmchat mode" + (f" {mode}" if mode else "")
        origin = Event(
            EventType.MESSAGE_CREATED,
            NOW,
            session.account.self_info,
            channel=session.event.channel,
            guild=session.event.guild,
            member=session.event.member,
            user=session.event.user,
            message=MessageObject(id=f"mode-command-{uuid4().hex}", content=content),
        )
        current = Session(session.account, MessageCreatedEvent(session.account, origin))
        return await execute(content, current)

    try:
        pending = user_plugin.disable()
        if pending:
            await asyncio.gather(*pending)
        yield SimpleNamespace(call=callback, configure=configure, resolved=resolved)
    finally:
        for plugin, enabled, _config in plugin_state:
            if enabled and not plugin.is_available:
                pending = plugin.enable()
                if pending:
                    await asyncio.gather(*pending)
        for plugin, enabled, original_config in plugin_state:
            if not enabled and plugin.is_available:
                pending = plugin.disable()
                if pending:
                    await asyncio.gather(*pending)
            plugin.config.clear()
            plugin.config.update(original_config)


@pytest_asyncio.fixture
async def mode_command(command_environment):
    async with native_mode_command(command_environment) as command:
        yield command


@pytest.mark.asyncio
async def test_query_missing_and_existing_normal_scope_creates_no_state_identity_or_requests(
    companion_db,
    native_transport,
    mode_command,
    monkeypatch,
):
    payloads = install_provider(monkeypatch, [])
    session = native_transport.session()
    for _ in range(2):
        result = await mode_command.call(session)
        assert NORMAL in result
    assert await native_rows(companion_db, ChatScope) == []
    assert await native_rows(companion_db, CompanionMode) == []
    assert await native_rows(companion_db, CompanionMember) == []
    assert await native_rows(companion_db, UnifiedUser) == []
    assert await native_rows(companion_db, Bind) == []
    native_scope = await scope(companion_db)
    selections = await native_rows(companion_db, ScopePersonaSelection)
    result = await mode_command.call(session)
    assert NORMAL in result
    assert len(await native_rows(companion_db, ChatScope)) == 1
    assert [(row.scope_id, row.persona_key) for row in await native_rows(companion_db, ScopePersonaSelection)] == [
        (row.scope_id, row.persona_key) for row in selections
    ]
    assert (await store.get_mode(native_scope.id)).revision == 0
    assert await native_rows(companion_db, CompanionMode) == []
    assert mode_command.resolved == payloads == []
    assert native_transport.network.lookups == native_transport.network.wire == []


@pytest.mark.asyncio
async def test_invalid_mode_and_normal_noop_on_unknown_scope_have_no_side_effects(
    companion_db,
    native_transport,
    mode_command,
    monkeypatch,
):
    payloads = install_provider(monkeypatch, [])
    worker = CompanionService(config(), accounts=lambda: [])
    dispose = install_companion_runtime(worker)
    try:
        assert "normal|companion" in await mode_command.call(native_transport.session(), "invented-mode")
        assert NORMAL in await mode_command.call(native_transport.session(), "normal")
        assert await native_rows(companion_db, ChatScope) == []
        assert await native_rows(companion_db, ScopePersonaSelection) == []
        assert await native_rows(companion_db, CompanionMode) == []
        assert await native_rows(companion_db, UnifiedUser) == []
        assert mode_command.resolved == payloads == []
    finally:
        dispose()
        await worker.aclose()


@pytest.mark.asyncio
async def test_mutation_requires_current_native_superuser_not_group_role_or_historical_operator(
    companion_db,
    native_transport,
    mode_command,
    monkeypatch,
):
    install_provider(monkeypatch, [])
    native_scope = await scope(companion_db)
    await store.set_mode(native_scope.id, "companion", persona_key="alpha", now=NOW)
    worker = CompanionService(config(), accounts=lambda: [])
    dispose = install_companion_runtime(worker)
    try:
        bob = native_transport.session(
            user="platform-bob",
            roles=("admin",),
            content="I was an operator earlier; switch the mode",
        )
        assert bob.member.roles[0].id == "admin"
        result = await mode_command.call(bob, "normal")
        assert "\u6743\u9650\u4e0d\u8db3" in result
        assert (await store.get_mode(native_scope.id)).mode == "companion"
        # Permission changes are read from current native configuration, not a
        # cached previous command result or a claim in the message contents.
        monkeypatch.setattr(EntariConfig.instance.basic, "superusers", {"onebot": ["platform-bob"]})
        result = await mode_command.call(native_transport.session(), "normal")
        assert "\u6743\u9650\u4e0d\u8db3" in result
        assert (await store.get_mode(native_scope.id)).mode == "companion"
        result = await mode_command.call(bob, "normal")
        assert NORMAL in result
        assert (await store.get_mode(native_scope.id)).mode == "normal"
        assert mode_command.resolved == []
    finally:
        dispose()
        await worker.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("channel_type", [ChannelType.TEXT, ChannelType.DIRECT])
async def test_model_called_mode_mutation_is_denied_even_with_real_operator_turn_context(
    companion_db,
    native_transport,
    mode_command,
    monkeypatch,
    channel_type,
):
    install_provider(monkeypatch, [])
    session = native_transport.session(
        channel="private:platform-alice" if channel_type == ChannelType.DIRECT else "companion-group",
        channel_type=channel_type,
    )
    prepared = await agent_turn_setup.prepare_agent_turn(
        config(),
        session,
        ChatIdentity("actual-operator", "Alice", ""),
        model_name="test-model",
        model_text="A technical request",
        raw_user_text="A technical request",
        content="A technical request",
        current_content=None,
        forwarded_messages=(),
        mentioned_participants=(),
        image_inputs=ImageInputs(),
        is_operator=True,
        warn=pytest.fail,
        tool_schemas=(),
    )
    worker = CompanionService(config(), accounts=lambda: [])
    dispose = install_companion_runtime(worker)
    try:
        assert prepared.agent_access.is_operator
        with agent_access_scope(prepared.agent_access):
            result = await mode_command.call(session, "companion")
            assert "\u6a21\u578b\u5de5\u5177\u4e0d\u80fd" in result
            assert NORMAL in await mode_command.call(session)
        assert await native_rows(companion_db, CompanionMode) == []
        assert await native_rows(companion_db, CompanionMember) == []
        assert mode_command.resolved == []
    finally:
        dispose()
        await worker.aclose()
        await prepared.lifecycle.finalize_agent_turn("completed")
        await prepared.image_inputs.aclose()


@pytest.mark.asyncio
async def test_direct_enable_persists_verified_actor_no_fabricated_turn_and_same_mode_is_noop(
    companion_db,
    native_transport,
    mode_command,
    monkeypatch,
):
    payloads = install_provider(monkeypatch, [])
    worker = CompanionService(config(), accounts=lambda: [])
    dispose = install_companion_runtime(worker)
    session = native_transport.session()
    try:
        result = await mode_command.call(session, "companion")
        assert COMPANION in result
        native_scope = (await native_rows(companion_db, ChatScope))[0]
        first = await store.get_mode(native_scope.id)
        assert first.mode == "companion"
        assert first.persona_key == "alpha"
        assert first.revision == 1
        members = await store.read_members(native_scope.id, "alpha", now=datetime.now(timezone.utc))
        assert len(members) == 1
        binds = await native_rows(companion_db, Bind)
        assert len(binds) == 1
        assert binds[0].platform == "onebot"
        assert binds[0].platform_id == session.user.id
        assert members[0].user_id == str(binds[0].bind_id)
        assert members[0].platform_user_id == session.user.id
        assert members[0].display_name == session.user.name
        assert members[0].version == 0
        assert members[0].last_turn_id == 0
        assert await native_rows(companion_db, AgentTurn) == []
        assert await native_rows(companion_db, Conversation) == []
        assert await native_rows(companion_db, CompanionChatEvidence) == []
        assert await native_rows(companion_db, CompanionReview) == []
        result = await mode_command.call(session, "companion")
        assert "\u672a\u91cd\u590d\u542f\u52a8" in result
        assert await store.get_mode(native_scope.id) == first
        assert len(await native_rows(companion_db, CompanionMember)) == 1
        assert len(await native_rows(companion_db, UnifiedUser)) == 1
        assert payloads == []
    finally:
        dispose()
        await worker.aclose()


@pytest.mark.asyncio
async def test_query_and_mode_persistence_match_exact_account_and_channel_after_runtime_replacement(
    companion_db,
    native_transport,
    mode_command,
    monkeypatch,
):
    payloads = install_provider(monkeypatch, [])
    first = CompanionService(config(), accounts=lambda: [])
    dispose_first = install_companion_runtime(first)
    second = None
    dispose_second = None
    other_account = Account(Login(sn=1, platform="onebot", user=User(id="another-bot")), ApiInfo(), [], EntariProtocol)

    async def reject_external(action, params):
        raise AssertionError("A mode query must not invoke the other account transport")

    monkeypatch.setattr(other_account.protocol, "call_api", reject_external)
    try:
        await mode_command.call(native_transport.session(), "companion")
        assert COMPANION in await mode_command.call(native_transport.session())
        assert NORMAL in await mode_command.call(native_transport.session(channel="another-channel"))
        source = native_transport.session()
        from arclet.entari import MessageCreatedEvent

        origin = Event(
            EventType.MESSAGE_CREATED,
            NOW,
            other_account.self_info,
            channel=source.channel,
            guild=source.guild,
            member=source.member,
            user=source.user,
            message=MessageObject(id="other-account-input", content="Query the mode"),
        )
        another_account_session = Session(other_account, MessageCreatedEvent(other_account, origin))
        assert NORMAL in await mode_command.call(another_account_session)
        assert len(await native_rows(companion_db, ChatScope)) == 1
        second = CompanionService(config(), accounts=lambda: [])
        dispose_second = install_companion_runtime(second)
        dispose_first()
        await first.aclose()
        assert COMPANION in await mode_command.call(native_transport.session())
        await mode_command.call(native_transport.session(), "normal")
        assert NORMAL in await mode_command.call(native_transport.session())
        assert NORMAL in await mode_command.call(native_transport.session(channel="another-channel"))
        assert len(await native_rows(companion_db, ChatScope)) == 1
        assert payloads == []
    finally:
        if dispose_second is not None:
            dispose_second()
        dispose_first()
        await first.aclose()
        if second is not None:
            await second.aclose()
        await other_account.protocol.session.close()


@pytest.mark.asyncio
async def test_direct_off_cancels_blocked_native_reflection_and_preserves_explicit_reminder(
    companion_db,
    native_transport,
    mode_command,
    monkeypatch,
):
    payloads = install_provider(monkeypatch, [])
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def reflection(**kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    worker = CompanionService(config(), accounts=lambda: [native_transport.account], reflection=reflection)
    dispose = install_companion_runtime(worker)
    try:
        await mode_command.call(native_transport.session(), "companion")
        native_scope = (await native_rows(companion_db, ChatScope))[0]
        async with companion_db.factory() as db, db.begin():
            db.add(
                ReminderTask(
                    task_ref="explicit-existing",
                    account_id=native_transport.account.self_id,
                    unified_user_id="explicit-user",
                    goal="An explicitly delegated task",
                    due_at=NOW.replace(tzinfo=None),
                )
            )
        await worker.start()
        await asyncio.wait_for(entered.wait(), 5)
        result = await mode_command.call(native_transport.session(), "normal")
        assert "\u660e\u786e\u63d0\u9192\u4e0d\u53d7\u5f71\u54cd" in result
        await asyncio.wait_for(worker.drain(), 5)
        assert cancelled.is_set()
        assert (await store.get_mode(native_scope.id)).mode == "normal"
        assert all(row.status != "active" for row in await native_rows(companion_db, CompanionReview))
        reminders = await native_rows(companion_db, ReminderTask)
        assert len(reminders) == 1
        assert reminders[0].task_ref == "explicit-existing"
        assert reminders[0].status == "active"
        assert reminders[0].goal == "An explicitly delegated task"
        assert payloads == []
        assert native_transport.network.wire == []
    finally:
        dispose()
        await worker.aclose()


@pytest.mark.asyncio
async def test_native_private_mode_query_and_opt_in_use_the_exact_verified_direct_scope(
    companion_db,
    native_transport,
    mode_command,
    monkeypatch,
):
    payloads = install_provider(monkeypatch, [])
    worker = CompanionService(config(), accounts=lambda: [])
    dispose = install_companion_runtime(worker)
    private = native_transport.session(channel="private:platform-alice", channel_type=ChannelType.DIRECT)
    try:
        assert private.event.guild is None
        assert NORMAL in await mode_command.call(private)
        assert NORMAL in await mode_command.call(private, "normal")
        assert await native_rows(companion_db, ChatScope) == []
        assert await native_rows(companion_db, UnifiedUser) == []
        assert await native_rows(companion_db, Bind) == []
        assert COMPANION in await mode_command.call(private, "companion")
        native_scope = (await native_rows(companion_db, ChatScope))[0]
        assert (
            native_scope.platform,
            native_scope.account_id,
            native_scope.guild_id,
            native_scope.channel_id,
        ) == ("onebot", native_transport.account.self_id, "", private.channel.id)
        assert COMPANION in await mode_command.call(private)
        assert NORMAL in await mode_command.call(native_transport.session())
        other_private = native_transport.session(
            channel="private:platform-bob",
            user="platform-bob",
            channel_type=ChannelType.DIRECT,
        )
        assert NORMAL in await mode_command.call(other_private)
        binds = await native_rows(companion_db, Bind)
        members = await native_rows(companion_db, CompanionMember)
        assert len(binds) == len(members) == 1
        assert binds[0].platform_id == private.user.id
        assert members[0].user_id == str(binds[0].bind_id)
        assert members[0].last_turn_id == 0
        assert await native_rows(companion_db, AgentTurn) == []
        assert await native_rows(companion_db, CompanionChatEvidence) == []
        assert NORMAL in await mode_command.call(private, "normal")
        assert (await store.get_mode(native_scope.id)).mode == "normal"
        assert len(await native_rows(companion_db, ChatScope)) == 1
        assert payloads == native_transport.network.lookups == native_transport.network.wire == []
    finally:
        dispose()
        await worker.aclose()


@pytest.mark.asyncio
async def test_command_load_unload_preserves_native_models_and_subsequent_provider_delivery(
    companion_db,
    native_transport,
    command_environment,
    monkeypatch,
):
    message = "The ordinary native reply still arrives after mode-command unload."
    payloads = install_provider(
        monkeypatch,
        [
            response(
                calls=[tool_call("send_msg", {"segments": [{"type": "text", "text": message}]}, "after-command-send")]
            ),
            response(
                calls=[
                    tool_call(
                        "finish_turn",
                        {"outcome": "delivered", "reason": "Native receipt confirmed"},
                        "after-command-finish",
                    )
                ]
            ),
        ],
    )
    worker = CompanionService(config(), accounts=lambda: [])
    dispose = install_companion_runtime(worker)
    session = native_transport.session(
        message="real-after-command", content="Please continue our ordinary conversation"
    )
    prepared = None
    try:
        for expected, requested in ((NORMAL, "companion"), (COMPANION, "normal"), (NORMAL, "normal")):
            async with native_mode_command(command_environment) as command:
                assert expected in await command.call(session)
                assert (COMPANION if requested == "companion" else NORMAL) in await command.call(session, requested)
            assert await execute("llmchat mode", session) is None
            assert payloads == native_transport.network.wire == []
        scopes = await native_rows(companion_db, ChatScope)
        binds = await native_rows(companion_db, Bind)
        assert len(scopes) == len(binds) == 1
        assert (await store.get_mode(scopes[0].id)).mode == "normal"
        assert await native_rows(companion_db, AgentTurn) == []
        assert await native_rows(companion_db, CompanionChatEvidence) == []
        prepared = await agent_turn_setup.prepare_agent_turn(
            config(),
            session,
            ChatIdentity(str(binds[0].bind_id), "Alice", ""),
            model_name="test-model",
            model_text=session.content,
            raw_user_text=session.content,
            content=session.content,
            current_content=None,
            forwarded_messages=(),
            mentioned_participants=(),
            image_inputs=ImageInputs(),
            warn=pytest.fail,
            tool_schemas=(),
        )
        assert prepared.conversation_mode == "normal"
        assert prepared.agent_access.scope_id == scopes[0].id

        async def no_mentions(_session, _reference):
            raise AssertionError("The ordinary text reply must not resolve a mention")

        async with native_tools() as dispatcher:
            register_send_msg(dispatcher, SendMsgToolContext(no_mentions))
            register_finish_turn(dispatcher)
            await generate_chat_response(
                prepared.chat_messages,
                system=prepared.system,
                model="test-model",
                channel_id=session.channel.id,
                ctx=Contexts({ITEM_SESSION: session}),
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
        assert len(payloads) == 2
        assert len(native_transport.network.wire) == 1
        assert native_transport.network.wire[0]["channel_id"] == session.channel.id
        assert native_transport.network.wire[0]["content"] == message
        assert [receipt.id for receipt in native_transport.network.receipts] == ["native-receipt-1"]
        assert prepared.delivery_state.confirmed_deliveries == 1
        await prepared.lifecycle.persist_delivered_text()
        await prepared.lifecycle.finalize_agent_turn("completed")
        turns = await native_rows(companion_db, AgentTurn)
        assert len(turns) == 1
        assert turns[0].status == "completed"
        assert turns[0].final_text == message
        assert len(await native_rows(companion_db, UserRelation)) == 1
        events = await native_rows(companion_db, AgentEvent)
        assert any(event.event_type == "tool_result" and event.status == "succeeded" for event in events)
        assert len(await native_rows(companion_db, UnifiedUser)) == 1
    finally:
        if prepared is not None:
            await prepared.lifecycle.finalize_agent_turn("cancelled")
            await prepared.image_inputs.aclose()
        dispose()
        await worker.aclose()
