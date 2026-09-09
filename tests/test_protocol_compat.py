"""Exercise Entari's direct transport through the installed OneBot wire encoder."""

from __future__ import annotations

import sys
import json
from uuid import uuid4
from types import SimpleNamespace
from pathlib import Path
from datetime import datetime
from importlib.util import find_spec, module_from_spec, spec_from_file_location

import pytest
from satori import User, Event, Login, Channel, EventType, ChannelType
from satori.model import MessageObject
from arclet.entari import Session, MessageCreatedEvent
from satori.client import Account, ApiInfo
from satori.server import Server
import pytest_asyncio
from arclet.letoderea import Scope
from satori.exception import ActionFailed
from arclet.entari.event.api import APIRequest, APIResponse
from satori.adapters.onebot11.forward import OneBot11ForwardConfig, OneBot11ForwardAdapter

from plugins.llm_chat import protocol_compat, reaction_feedback


@pytest.fixture
def boundary_modules(monkeypatch):
    monkeypatch.setattr(reaction_feedback, "_LLONEBOT_REACTION_BACKENDS", {})

    def load(path):
        name = f"_protocol_boundary_{uuid4().hex}"
        spec = spec_from_file_location(name, path)
        assert spec is not None
        assert spec.loader is not None
        module = module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        return module

    server_spec = find_spec("entari_plugin_server")
    assert server_spec is not None
    assert server_spec.origin is not None
    # Import the real direct transport without executing the server plugin entry.
    return SimpleNamespace(
        compat=protocol_compat,
        reaction=reaction_feedback,
        direct=load(Path(server_spec.origin).with_name("patch.py")),
    )


@pytest.fixture
def direct_transport(boundary_modules):
    async def build():
        restore = boundary_modules.compat.install_entari_internal_bridge()
        server = Server()
        adapter = OneBot11ForwardAdapter(OneBot11ForwardConfig("ws://127.0.0.1:1"))
        login = Login(sn=0, platform="onebot", adapter="onebot", user=User("protocol-boundary-bot"))
        adapter.logins[login.user.id] = login
        server.apply(adapter)
        wire = []
        methods = []
        rejected = set()
        reactions = set()

        class OneBotPeer:
            async def send_str(self, data):
                frame = json.loads(data)
                action, params = frame["action"], frame["params"]
                wire.append((action, params))
                if action in rejected:
                    result = {"status": "failed", "retcode": 1400, "message": "rejected by adapter"}
                elif action == "get_version_info":
                    result = {"status": "ok", "data": {"app_name": "LLOneBot"}}
                elif action == "set_msg_emoji_like":
                    # A strict wire peer reproduces the production missing-ID error.
                    if set(params) != {"message_id", "emoji_id", "set"}:
                        result = {"status": "failed", "retcode": 1400, "message": "$.message_id missing required value"}
                    else:
                        key = (params["message_id"], params["emoji_id"])
                        if params["set"]:
                            reactions.add(key)
                        else:
                            reactions.discard(key)
                        result = {"status": "ok", "data": {}}
                elif action == "get_forward_msg":
                    if params.get("message_id") != "opaque-forward-id":
                        result = {"status": "failed", "retcode": 1400, "message": "missing forward ID"}
                    else:
                        result = {"status": "ok", "data": {"messages": [{"content": "forward content"}]}}
                elif action == "get_group_info":
                    result = {"status": "ok", "data": {"group_id": params["group_id"], "group_name": "channel"}}
                elif action == "configure":
                    result = {"status": "ok", "data": {"applied": params["options"]["enabled"] is False}}
                else:
                    raise AssertionError(f"unexpected OneBot action: {action}")
                adapter.response_waiters[frame["echo"]].set_result(result)

        adapter.connection = OneBotPeer()
        route = adapter.routes["internal/*"]

        async def record_method(request):
            methods.append(request.origin.method)
            return await route(request)

        adapter.routes["internal/*"] = record_method
        account = Account(login, ApiInfo(), [], boundary_modules.direct.DirectAdapterProtocol)
        account.protocol.server = server
        origin = Event(
            EventType.MESSAGE_CREATED,
            datetime.now(),
            login,
            channel=Channel("42", ChannelType.TEXT),
            user=User("actor"),
            message=MessageObject("12345", "status"),
        )
        session = Session(account, MessageCreatedEvent(account, origin))
        return SimpleNamespace(
            session=session,
            account=account,
            server=server,
            wire=wire,
            methods=methods,
            rejected=rejected,
            reactions=reactions,
            restore=restore,
        )

    return build


@pytest_asyncio.fixture
async def transport(direct_transport):
    harness = await direct_transport()
    try:
        yield harness
    finally:
        harness.restore()
        await harness.account.protocol.session.close()
        harness.server._tempdir.cleanup()


@pytest.mark.asyncio
async def test_reactions_and_internal_parameters_reach_real_onebot_transport(boundary_modules, transport):
    warnings = []
    feedback = boundary_modules.reaction.MessageReactionFeedback(transport.session, warnings.append)
    await feedback.set_stage("processing")
    await feedback.finish("success")
    assert feedback.terminal
    assert warnings == []
    assert transport.reactions == {(12345, 124)}
    assert transport.wire == [
        ("get_version_info", {}),
        ("set_msg_emoji_like", {"message_id": 12345, "emoji_id": 125, "set": True}),
        ("set_msg_emoji_like", {"message_id": 12345, "emoji_id": 125, "set": False}),
        ("set_msg_emoji_like", {"message_id": 12345, "emoji_id": 124, "set": True}),
    ]

    forwarded = await transport.session.internal("get_forward_msg", message_id="opaque-forward-id")
    assert forwarded == {"messages": [{"content": "forward content"}]}
    options = {"enabled": False, "count": 0, "label": "\u4e2d\u6587", "empty": None, "items": [False, 0]}
    assert await transport.session.internal("configure", method="PUT", options=options) == {"applied": True}
    assert transport.wire[-1] == ("configure", {"options": options})
    assert transport.methods == ["POST"] * 5 + ["PUT"]

    channel = await transport.account.protocol.channel_get("42")
    assert channel.id == "42"
    assert channel.name == "channel"
    assert transport.wire[-1] == ("get_group_info", {"group_id": 42})


@pytest.mark.asyncio
async def test_internal_errors_notify_observers_and_reaction_feedback_fails_open(boundary_modules, transport):
    scope = Scope.of()
    responses = []

    async def observed(event: APIResponse):
        responses.append(event)

    scope.register(observed, event=APIResponse)
    transport.rejected.add("set_msg_emoji_like")
    try:
        warnings = []
        feedback = boundary_modules.reaction.MessageReactionFeedback(transport.session, warnings.append)
        await feedback.set_stage("processing")
        await feedback.set_stage("thinking")
        await feedback.finish("failed")
        assert feedback.terminal
        assert len(warnings) == 1
        assert transport.reactions == set()
        assert [action for action, _ in transport.wire] == ["get_version_info", "set_msg_emoji_like"]
        assert [(response.name, response.success) for response in responses] == [
            ("internal", True),
            ("internal", False),
        ]
        assert isinstance(responses[-1].result, ActionFailed)
        with pytest.raises(ActionFailed):
            await transport.session.internal("set_msg_emoji_like", message_id=12345, emoji_id=125, set=False)
    finally:
        scope.dispose()


@pytest.mark.asyncio
async def test_internal_request_override_and_reload_ownership(boundary_modules, transport):
    scope = Scope.of()
    responses = []

    async def override(event: APIRequest):
        if event.name == "internal" and event.params["action"] == "get_version_info":
            return {"app_name": "intercepted"}

    async def observed(event: APIResponse):
        responses.append(event)

    scope.register(override, event=APIRequest)
    scope.register(observed, event=APIResponse)
    second_restore = boundary_modules.compat.install_entari_internal_bridge()
    custom = transport.account.custom(protocol_cls=type(transport.account.protocol))
    custom.protocol.server = transport.server
    try:
        transport.restore()
        assert await custom.protocol.internal("get_version_info") == {"app_name": "intercepted"}
        assert transport.wire == []
        assert responses[-1].success
        assert responses[-1].result == {"app_name": "intercepted"}
        await custom.protocol.internal("set_msg_emoji_like", message_id=12345, emoji_id=125, set=False)
        assert transport.wire == [("set_msg_emoji_like", {"message_id": 12345, "emoji_id": 125, "set": False})]
    finally:
        second_restore()
        scope.dispose()
        await custom.protocol.session.close()
