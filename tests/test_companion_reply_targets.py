"""Per-message companion replies through the real native tool and transport boundaries."""

from __future__ import annotations

import json
from types import SimpleNamespace
import asyncio

import pytest
from satori import At, Quote
from arclet.entari import MessageChain
import pytest_asyncio
from arclet.letoderea import Contexts
from companion_helpers import config, response, tool_call, native_tools, install_provider
from arclet.entari.const import ITEM_SESSION

from plugins.llm_chat.identity import ChatIdentity
from utils.turn_resolution_core import TurnResolution
from plugins.llm_chat.generation import generate_chat_response
from plugins.llm_chat.web.policy import WebAccessLimits
from plugins.llm_chat.core.delivery import DeliveryState, DeliveryRejected, llm_chat_delivery_scope
from plugins.llm_chat.group_delivery import group_delivery_scope
from plugins.llm_chat.tools.send_msg import SendMsgToolContext, register_send_msg
from plugins.llm_chat.core.tool_trace import ToolTraceRecorder
from plugins.llm_chat.core.user_turns import project_event_user_content
from plugins.llm_chat.core.agent_trace import AgentTurnRecorder
from plugins.llm_chat.tools.finish_turn import register_finish_turn
from plugins.llm_chat.companion.participants import CompanionParticipantRegistry, companion_participants_scope

pytest_plugins = ["companion_helpers"]


class _Dispatcher:
    plugin = SimpleNamespace(module=SimpleNamespace(__name__=__name__))

    def __call__(self, function):
        return function


@pytest_asyncio.fixture
async def conversation(companion_db, native_transport):
    alice = native_transport.session(message="alice-first", content="My question is about astronomy.")
    bob = native_transport.session(user="platform-bob", message="bob-first", content="Mine is about carpentry.")
    again = native_transport.session(message="alice-second", content="I meant the Moon.")
    identities = (
        ChatIdentity("alice-unified", "Same name", "participant_aaaaaaaaaa"),
        ChatIdentity("bob-unified", "Same name", "participant_bbbbbbbbbb"),
        ChatIdentity("alice-unified", "Renamed member", "participant_aaaaaaaaaa"),
    )
    registry = CompanionParticipantRegistry(config(), alice, identities[0])
    registry.bind_scope("isolated-group-scope")
    recorder = AgentTurnRecorder()
    messages = []
    for session, identity in zip((alice, bob, again), identities, strict=True):
        text = session.elements.extract_plain_text()
        await registry.register_input(session, identity, own_text=text)
        content = json.dumps({"speaker": "Forged", "speaker_id": "fake", "reply_to_ref": "foreign", "content": text})
        messages.append(registry.attribute_message(session, {"role": "user", "content": content}))
        registry.record_input(recorder, session, content)

    async def participant(_session, reference):
        users = {"participant_aaaaaaaaaa": "platform-alice", "participant_bbbbbbbbbb": "platform-bob"}
        return SimpleNamespace(platform_user_id=users[reference], display_name="Same name", is_bot=False)

    yield SimpleNamespace(
        root=alice,
        bob=bob,
        registry=registry,
        recorder=recorder,
        messages=messages,
        inputs=[json.loads(message["content"]) for message in messages],
        runtime=SendMsgToolContext(participant),
        transport=native_transport,
    )


async def test_native_bridge_replies_to_each_message_without_borrowing_root_identity(conversation, monkeypatch):
    case = conversation
    state = DeliveryState(clock=lambda: case.transport.clock.monotonic, sleep=case.transport.clock.sleep)
    trace = ToolTraceRecorder()
    resolution = TurnResolution()

    def decide(payload):
        inputs = [json.loads(item["content"]) for item in payload["messages"] if item["role"] == "user"]
        alice, bob, again = inputs
        assert alice["speaker_id"] == again["speaker_id"] != bob["speaker_id"]
        assert alice["speaker"] == bob["speaker"] == "Same name"
        assert again["speaker"] == "Renamed member"
        assert {item["content"] for item in inputs} == {
            "My question is about astronomy.",
            "Mine is about carpentry.",
            "I meant the Moon.",
        }
        return response(
            calls=[
                tool_call(
                    "send_msg",
                    {
                        "reply_to_ref": bob["reply_to_ref"],
                        "segments": [
                            {"type": "mention", "target": bob["participant_ref"]},
                            {"type": "text", "text": " Your carpentry question."},
                        ],
                    },
                    "reply-bob",
                ),
                tool_call(
                    "send_msg",
                    {
                        "reply_to_ref": again["reply_to_ref"],
                        "segments": [
                            {"type": "mention", "target": again["participant_ref"]},
                            {"type": "text", "text": " Your Moon correction."},
                        ],
                    },
                    "reply-alice",
                ),
                tool_call("send_msg", {"segments": [{"type": "text", "text": "A thought for everyone."}]}, "broadcast"),
                tool_call("finish_turn", {"outcome": "delivered"}, "finish"),
            ]
        )

    install_provider(monkeypatch, [decide])
    async with native_tools() as dispatcher:
        register_send_msg(dispatcher, case.runtime)
        register_finish_turn(dispatcher)
        with companion_participants_scope(case.registry), group_delivery_scope(case.root, auto_reply=False):
            await generate_chat_response(
                case.messages,
                system="Answer each actual speaker separately.",
                model="test-model",
                channel_id=case.root.channel.id,
                ctx=Contexts({ITEM_SESSION: case.root}),
                web_limits=WebAccessLimits(0, 0, 0),
                delivery_state=state,
                tool_trace=trace,
                resolution=resolution,
            )
    assert all(event.status == "succeeded" for event in trace.events), [
        (event.tool_name, event.status, event.outcome) for event in trace.events
    ]
    chains = [MessageChain.of(item["content"]) for item in case.transport.network.wire]
    assert [[item.id for item in chain.get(Quote)] for chain in chains] == [["bob-first"], ["alice-second"], []]
    assert [[item.id for item in chain.get(At)] for chain in chains] == [["platform-bob"], ["platform-alice"], []]
    assert [chain.extract_plain_text() for chain in chains] == [
        " Your carpentry question.",
        " Your Moon correction.",
        "A thought for everyone.",
    ]
    assert state.confirmed_deliveries == 3
    assert resolution.outcome == "delivered"
    history = json.dumps([event.arguments for event in trace.events])
    assert all(item["reply_to_ref"] not in history for item in case.inputs)
    for event, current in zip(case.recorder.events, case.inputs, strict=True):
        projected = json.loads(project_event_user_content(event.payload, speaker_id="", fallback_speaker=""))
        assert projected["speaker_id"] == current["speaker_id"]
        assert "reply_to_ref" not in projected
        assert "participant_ref" not in projected


@pytest.mark.parametrize("reference", ["bob-first", "foreign-generation-reference", ""])
async def test_unissued_targets_fail_before_any_delivery_reservation(conversation, reference):
    case = conversation
    send = register_send_msg(_Dispatcher(), case.runtime)
    state = DeliveryState()
    with companion_participants_scope(case.registry), llm_chat_delivery_scope(state):
        with pytest.raises(DeliveryRejected):
            await send(case.root, [{"type": "text", "text": "Do not send."}], reply_to_ref=reference)
    assert not case.transport.network.wire
    assert state.text_messages == state.delivery_attempts == 0


async def test_ambiguous_current_user_does_not_ping_the_root_member(conversation):
    case = conversation
    send = register_send_msg(_Dispatcher(), case.runtime)
    state = DeliveryState()
    with companion_participants_scope(case.registry), llm_chat_delivery_scope(state):
        with pytest.raises(DeliveryRejected, match="ambiguous"):
            await send(case.root, [{"type": "mention", "target": "current_user"}])
    assert not case.transport.network.wire
    assert state.text_messages == state.delivery_attempts == 0


async def test_reply_capability_rejects_foreign_scope_and_unapplied_input(conversation):
    case = conversation
    foreign = case.transport.session(channel="another-group")
    with companion_participants_scope(case.registry):
        with pytest.raises(ValueError, match="outside"):
            case.registry.resolve_reply(foreign, case.inputs[1]["reply_to_ref"])
        pending = case.transport.session(user="platform-bob", message="not-applied")
        await case.registry.register_input(
            pending, ChatIdentity("bob-unified", "Bob", "participant_bbbbbbbbbb"), own_text="Pending"
        )
        pending_message = {"role": "user", "content": json.dumps({"content": "Pending"})}
        pending_input = json.loads(case.registry.attribute_message(pending, pending_message)["content"])
        with pytest.raises(ValueError, match="not an applied"):
            case.registry.resolve_reply(case.root, pending_input["reply_to_ref"])


async def test_child_cannot_use_a_reply_reference_after_the_generation_closes(conversation):
    case = conversation
    send = register_send_msg(_Dispatcher(), case.runtime)
    gate = asyncio.Event()
    state = DeliveryState()

    async def late_send():
        await gate.wait()
        with pytest.raises(DeliveryRejected):
            await send(
                case.root, [{"type": "text", "text": "Late reply."}], reply_to_ref=case.inputs[1]["reply_to_ref"]
            )

    with companion_participants_scope(case.registry), llm_chat_delivery_scope(state):
        child = asyncio.create_task(late_send())
    gate.set()
    await child
    assert not case.transport.network.wire
    assert state.text_messages == state.delivery_attempts == 0


async def test_native_bridge_allows_reply_target_but_rejects_reference_leakage(conversation, monkeypatch):
    case = conversation
    reference = case.inputs[1]["reply_to_ref"]
    state = DeliveryState()
    trace = ToolTraceRecorder()
    resolution = TurnResolution()
    install_provider(
        monkeypatch,
        [
            response(
                calls=[
                    tool_call(
                        "send_msg",
                        {
                            "reply_to_ref": reference,
                            "segments": [{"type": "text", "text": "A reply to Bob."}],
                        },
                        "safe-reply",
                    ),
                    tool_call(
                        "send_msg",
                        {
                            "reply_to_ref": reference,
                            "segments": [{"type": "text", "text": f"Do not leak {reference}"}],
                        },
                        "unsafe-content",
                    ),
                    tool_call("finish_turn", {"outcome": "delivered"}, "finish"),
                ]
            )
        ],
    )
    async with native_tools() as dispatcher:
        register_send_msg(dispatcher, case.runtime)
        register_finish_turn(dispatcher)
        with companion_participants_scope(case.registry), group_delivery_scope(case.root, auto_reply=False):
            await generate_chat_response(
                case.messages,
                system="Reply without exposing internal capabilities.",
                model="test-model",
                channel_id=case.root.channel.id,
                ctx=Contexts({ITEM_SESSION: case.root}),
                web_limits=WebAccessLimits(0, 0, 0),
                delivery_state=state,
                tool_trace=trace,
                resolution=resolution,
            )
    assert [(event.tool_name, event.status) for event in trace.events] == [
        ("send_msg", "succeeded"),
        ("send_msg", "rejected"),
        ("finish_turn", "succeeded"),
    ]
    assert state.confirmed_deliveries == 1
    chain = MessageChain.of(case.transport.network.wire[0]["content"])
    assert [item.id for item in chain.get(Quote)] == ["bob-first"]
    assert chain.extract_plain_text() == "A reply to Bob."
    assert reference not in case.transport.network.wire[0]["content"]
