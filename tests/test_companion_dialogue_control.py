"""Real native bridge contracts for companion-only staged conversation control."""

from __future__ import annotations

import json
import asyncio

import pytest
from jsonschema import Draft202012Validator
from agno.run.agent import RunOutput
from arclet.letoderea import Contexts
from companion_helpers import config, response, tool_call, native_tools, install_provider
from arclet.entari.const import ITEM_SESSION
from agno.models.response import ToolExecution
from agno.run.requirement import RunRequirement
from entari_plugin_llm.tools import available_functions
import entari_plugin_llm.service as llm_service

from plugins.llm_chat.identity import ChatIdentity
from utils.turn_resolution_core import TurnResolution, turn_resolution_scope
from plugins.llm_chat.generation import generate_chat_response
from plugins.llm_chat.web.policy import WebAccessLimits
from plugins.llm_chat.agno_compat import agno_tool_call_limit_scope
from utils.companion_core.dialogue import DialogueDecision
from plugins.llm_chat.agent_context import AUTONOMOUS_TOOL_CAPABILITIES, AgentAccessContext, agent_access_scope
from plugins.llm_chat.core.delivery import DeliveryState, DeliveryRejected, llm_chat_delivery_scope
from plugins.llm_chat.reminders.tool import register_reminder_tools
from plugins.llm_chat.tools.send_msg import SendMsgToolContext, register_send_msg
from plugins.llm_chat.core.tool_trace import ToolTraceRecorder, llm_chat_tool_trace_scope
from plugins.llm_chat.core.agent_trace import AgentTurnRecorder
from plugins.llm_chat.companion.context import companion_prompt
from plugins.llm_chat.tools.finish_turn import register_finish_turn
from plugins.llm_chat.tools.pin_context import register_pin_context
from plugins.llm_chat.companion.participants import CompanionParticipantRegistry, companion_participants_scope
from plugins.llm_chat.core.tool_trace_policy import DeliverySnapshot
from plugins.llm_chat.tools.companion_dialogue import companion_dialogue, register_companion_dialogue
from plugins.llm_chat.companion.dialogue_control import dialogue_control_scope, current_dialogue_control

pytest_plugins = ["companion_helpers"]


def requirement(name, arguments, call_id="dialogue-control"):
    return RunRequirement(
        ToolExecution(
            tool_name=name,
            tool_args=arguments,
            tool_call_id=call_id,
            external_execution_required=True,
        )
    )


@pytest.mark.asyncio
async def test_normal_native_declarations_stay_identical_and_control_is_not_callable():
    async with native_tools() as dispatcher:
        register_finish_turn(dispatcher)
        before = llm_service.get_agno_tools()
        with llm_chat_tool_trace_scope(ToolTraceRecorder()):
            scoped_before = llm_service.get_agno_tools()
        register_companion_dialogue(dispatcher)
        assert llm_service.get_agno_tools() == before
        with llm_chat_tool_trace_scope(ToolTraceRecorder()), agno_tool_call_limit_scope(8):
            assert llm_service.get_agno_tools() == scoped_before
            blocked = requirement("companion_dialogue", {"action": "wait"})
            assert await llm_service.run_llm_tools(RunOutput(requirements=[blocked])) is True
            result = json.loads(blocked.external_execution_result)
            assert result["ok"] is False
            assert "not allowed" in result["error"]
        with pytest.raises(DeliveryRejected, match="live companion dialogue"):
            await companion_dialogue("wait")


@pytest.mark.asyncio
async def test_native_control_schema_and_default_observe_decision():
    async with native_tools() as dispatcher:
        register_companion_dialogue(dispatcher)
        function = available_functions["companion_dialogue"][1]
        validator = Draft202012Validator(function.parameters)
        assert validator.is_valid({"action": "observe"})
        assert validator.is_valid({"action": "wait", "seconds": 45})
        assert validator.is_valid({"action": "end"})
        assert validator.is_valid({"action": "observe", "engaged_speakers": ["m_0123456789"]})
        assert validator.is_valid({"action": "observe", "engaged_speakers": None})
        assert not validator.is_valid({"action": "observe", "engaged_speakers": "m_0123456789"})
        assert not validator.is_valid({"action": "send"})
        assert not validator.is_valid({"action": "wait", "seconds": True})
        with (
            dialogue_control_scope() as control,
            llm_chat_tool_trace_scope(ToolTraceRecorder()),
            agno_tool_call_limit_scope(8),
        ):
            assert control.decision == DialogueDecision("observe", 30)
            assert function in llm_service.get_agno_tools()
            staged = requirement("companion_dialogue", {"action": "observe"})
            assert await llm_service.run_llm_tools(RunOutput(requirements=[staged])) is True
            assert json.loads(staged.external_execution_result)["ok"] is True
            assert control.decision == DialogueDecision("observe", 30)
        assert not control.live
        assert current_dialogue_control() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["observe", "wait", "end"])
async def test_control_stages_only_and_does_not_prevent_native_silent_finish(action):
    trace = ToolTraceRecorder()
    state = DeliveryState()
    resolution = TurnResolution()
    async with native_tools() as dispatcher:
        register_companion_dialogue(dispatcher)
        register_finish_turn(dispatcher)
        with (
            dialogue_control_scope() as control,
            llm_chat_tool_trace_scope(trace),
            llm_chat_delivery_scope(state),
            turn_resolution_scope(resolution),
            agno_tool_call_limit_scope(8),
        ):
            staged = requirement("companion_dialogue", {"action": action, "seconds": 45})
            assert await llm_service.run_llm_tools(RunOutput(requirements=[staged])) is True
            assert control.decision == DialogueDecision(action, 45)
            assert not resolution.explicit
            assert state.delivery_attempts == state.confirmed_deliveries == 0
            assert [(event.tool_name, event.effect) for event in trace.events] == [("companion_dialogue", "none")]
            assert trace.events[0].arguments == {"action": action, "seconds": 45}
            assert trace.events[0].outcome == {"action": action, "seconds": 45}
            silent = requirement("finish_turn", {"outcome": "silent"}, "silent-after-control")
            assert await llm_service.run_llm_tools(RunOutput(requirements=[silent])) is False
            assert json.loads(silent.external_execution_result)["ok"] is True
            assert resolution.outcome == "silent"
        assert control.decision == DialogueDecision(action, 45)


@pytest.mark.asyncio
async def test_failed_control_does_not_become_an_external_write_or_block_silence():
    trace = ToolTraceRecorder()
    state = DeliveryState()
    resolution = TurnResolution()
    async with native_tools() as dispatcher:
        register_finish_turn(dispatcher)
        with (
            dialogue_control_scope(),
            llm_chat_tool_trace_scope(trace),
            llm_chat_delivery_scope(state),
            turn_resolution_scope(resolution),
            agno_tool_call_limit_scope(8),
        ):
            call = trace.start("companion_dialogue", {"action": "wait", "seconds": 10})
            trace.finish_error(
                call, RuntimeError("Control did not stage"), before=DeliverySnapshot(), after=DeliverySnapshot()
            )
            assert trace.events[0].status == "failed"
            assert trace.events[0].effect == "none"
            silent = requirement("finish_turn", {"outcome": "silent"})
            assert await llm_service.run_llm_tools(RunOutput(requirements=[silent])) is False
            assert json.loads(silent.external_execution_result)["ok"] is True
            assert resolution.outcome == "silent"


@pytest.mark.asyncio
@pytest.mark.parametrize(("action", "seconds"), [("invalid", 30), ("wait", True), ("wait", 0), ("wait", 3601)])
async def test_invalid_control_never_replaces_the_last_valid_decision(action, seconds):
    with dialogue_control_scope() as control:
        await companion_dialogue("wait", 45)
        with pytest.raises(DeliveryRejected):
            await companion_dialogue(action, seconds)
        assert control.decision == DialogueDecision("wait", 45)


@pytest.mark.asyncio
async def test_closed_scope_fences_inherited_native_tool_tasks():
    ready = asyncio.Event()
    release = asyncio.Event()
    trace = ToolTraceRecorder()
    async with native_tools() as dispatcher:
        register_companion_dialogue(dispatcher)
        with (
            dialogue_control_scope() as control,
            llm_chat_tool_trace_scope(trace),
            agno_tool_call_limit_scope(8),
        ):
            llm_service.get_agno_tools()

            async def late_child():
                ready.set()
                await release.wait()
                assert current_dialogue_control() is None
                assert "companion_dialogue" not in {function.name for function in llm_service.get_agno_tools()}
                with pytest.raises(ValueError, match="no longer active"):
                    control.stage("wait", 60)
                late = requirement("companion_dialogue", {"action": "wait", "seconds": 60}, "late-control")
                assert await llm_service.run_llm_tools(RunOutput(requirements=[late])) is True
                assert json.loads(late.external_execution_result)["ok"] is False

            child = asyncio.create_task(late_child())
            await ready.wait()
        release.set()
        await child
        assert control.decision == DialogueDecision()
        assert trace.events[-1].effect == "none"


@pytest.mark.asyncio
async def test_generation_tool_snapshot_does_not_gain_control_from_a_later_scope():
    trace = ToolTraceRecorder()
    async with native_tools() as dispatcher:
        register_finish_turn(dispatcher)
        register_companion_dialogue(dispatcher)
        with llm_chat_tool_trace_scope(trace), agno_tool_call_limit_scope(8):
            before = llm_service.get_agno_tools()
            with dialogue_control_scope() as control:
                assert llm_service.get_agno_tools() == before
                injected = requirement("companion_dialogue", {"action": "wait"})
                assert await llm_service.run_llm_tools(RunOutput(requirements=[injected])) is True
                assert json.loads(injected.external_execution_result)["ok"] is False
                assert control.decision == DialogueDecision()


@pytest.mark.asyncio
async def test_control_continues_real_native_generation_before_confirmed_send(native_transport, monkeypatch):
    state = DeliveryState()
    trace = ToolTraceRecorder()
    resolution = TurnResolution()
    message = "That is a lovely color combination."

    def after_control(payload):
        assert not resolution.explicit
        assert state.delivery_attempts == 0
        assert current_dialogue_control().decision == DialogueDecision("wait", 45)
        results = [item for item in payload["messages"] if item["role"] == "tool"]
        assert len(results) == 1
        result = json.loads(results[0]["content"])
        assert result["ok"] is True
        assert result["data"] == {"action": "wait", "seconds": 45}
        return response(
            calls=[
                tool_call("send_msg", {"segments": [{"type": "text", "text": message}]}, "dialogue-send"),
                tool_call("finish_turn", {"outcome": "delivered"}, "dialogue-finish"),
            ]
        )

    payloads = install_provider(
        monkeypatch,
        [
            response(calls=[tool_call("companion_dialogue", {"action": "wait", "seconds": 45}, "dialogue-stage")]),
            after_control,
        ],
    )
    session = native_transport.session()
    ctx = Contexts({ITEM_SESSION: session})

    async def no_mentions(_session, _reference):
        raise AssertionError("Plain companion text must not resolve a participant")

    async with native_tools() as dispatcher:
        register_companion_dialogue(dispatcher)
        register_send_msg(dispatcher, SendMsgToolContext(no_mentions))
        register_finish_turn(dispatcher)
        with dialogue_control_scope() as control:
            await generate_chat_response(
                [{"role": "user", "content": "I am thinking of blue and yellow."}],
                system=companion_prompt({"life": {}}),
                model="test-model",
                channel_id=session.channel.id,
                ctx=ctx,
                web_limits=WebAccessLimits(0, 0, 0),
                delivery_state=state,
                tool_trace=trace,
                resolution=resolution,
            )
            assert control.decision == DialogueDecision("wait", 45)
        assert not control.live
    assert len(payloads) == 2
    assert "companion_dialogue" in {item["function"]["name"] for item in payloads[0]["tools"]}
    assert resolution.outcome == "delivered"
    assert state.confirmed_deliveries == 1
    assert len(native_transport.network.wire) == 1
    assert [(event.tool_name, event.effect) for event in trace.events] == [
        ("companion_dialogue", "none"),
        ("send_msg", "confirmed"),
        ("finish_turn", "none"),
    ]


@pytest.mark.asyncio
async def test_ambient_message_authority_only_exposes_social_tools_and_live_control():
    ambient = AgentAccessContext(
        scope_id=1,
        session_id=2,
        turn_id=3,
        user_id="alice",
        raw_user_text="",
        is_operator=False,
        trigger_kind="message",
        capability_names=AUTONOMOUS_TOOL_CAPABILITIES | {"companion.ambient", "reminder", "reminder.manage"},
    )
    async with native_tools() as dispatcher:
        register_companion_dialogue(dispatcher)
        register_finish_turn(dispatcher)
        register_reminder_tools(dispatcher)
        register_pin_context(dispatcher)
        with (
            dialogue_control_scope(),
            agent_access_scope(ambient),
            llm_chat_tool_trace_scope(ToolTraceRecorder()),
            agno_tool_call_limit_scope(8),
        ):
            names = {function.name for function in llm_service.get_agno_tools()}
            assert {"companion_dialogue", "finish_turn"} <= names
            assert not {"reminder", "pin_context"} & names
            blocked = [requirement("reminder", {}, "ambient-reminder"), requirement("pin_context", {}, "ambient-pin")]
            assert await llm_service.run_llm_tools(RunOutput(requirements=blocked)) is True
            assert all(json.loads(item.external_execution_result)["ok"] is False for item in blocked)
        directed = AgentAccessContext(
            scope_id=1,
            session_id=2,
            turn_id=4,
            user_id="alice",
            raw_user_text="Remember my actual request",
            capability_names=frozenset({"reminder"}),
        )
        with (
            agent_access_scope(directed),
            llm_chat_tool_trace_scope(ToolTraceRecorder()),
            agno_tool_call_limit_scope(8),
        ):
            names = {function.name for function in llm_service.get_agno_tools()}
            assert {"reminder", "pin_context"} <= names
            assert "companion_dialogue" not in names


@pytest.mark.asyncio
async def test_captured_control_registration_cannot_be_rebound_by_name():
    async with native_tools() as dispatcher:
        register_companion_dialogue(dispatcher)
        captured = available_functions["companion_dialogue"]
        with dialogue_control_scope() as control:
            with llm_chat_tool_trace_scope(ToolTraceRecorder()), agno_tool_call_limit_scope(8):
                assert captured[1] in llm_service.get_agno_tools()
                register_companion_dialogue(dispatcher)
                replacement = available_functions["companion_dialogue"]
                assert replacement is not captured
                assert "companion_dialogue" not in {function.name for function in llm_service.get_agno_tools()}
                stale = requirement("companion_dialogue", {"action": "wait", "seconds": 45})
                assert await llm_service.run_llm_tools(RunOutput(requirements=[stale])) is True
                result = json.loads(stale.external_execution_result)
                assert result["ok"] is False
                assert "registration is no longer available" in result["error"]
                assert control.decision == DialogueDecision()
            with llm_chat_tool_trace_scope(ToolTraceRecorder()), agno_tool_call_limit_scope(8):
                assert replacement[1] in llm_service.get_agno_tools()
                fresh = requirement("companion_dialogue", {"action": "wait", "seconds": 45}, "fresh-control")
                assert await llm_service.run_llm_tools(RunOutput(requirements=[fresh])) is True
                assert json.loads(fresh.external_execution_result)["ok"] is True
                assert control.decision == DialogueDecision("wait", 45)


@pytest.mark.asyncio
async def test_native_engagement_uses_only_applied_actual_speakers_and_confirmed_delivery(
    companion_db, native_transport
):
    alice = native_transport.session(message="engagement-alice", content="I like blue.")
    bob = native_transport.session(user="platform-bob", message="engagement-bob", content="Perhaps green.")
    queued = native_transport.session(user="platform-charlie", message="queued-charlie", content="A later thought.")
    registry = CompanionParticipantRegistry(config(), alice, ChatIdentity("alice", "Alice", ""))
    registry.bind_scope("scope-engagement")
    recorder = AgentTurnRecorder()
    for session, user, name, applied in (
        (alice, "alice", "Alice", True),
        (bob, "bob", "Bob", True),
        (queued, "charlie", "Charlie", False),
    ):
        await registry.register_input(
            session, ChatIdentity(user, name, ""), own_text=name + "'s contribution", directed=False
        )
        if applied:
            registry.record_input(recorder, session, json.dumps({"content": name + "'s contribution"}))
    alice_id = registry.host_author(alice)["speaker_id"]
    bob_id = registry.host_author(bob)["speaker_id"]
    queued_id = registry.host_author(queued)["speaker_id"]
    trace = ToolTraceRecorder()
    state = DeliveryState()
    ambient = AgentAccessContext(
        scope_id=1,
        session_id=2,
        turn_id=3,
        user_id="alice",
        capability_names=AUTONOMOUS_TOOL_CAPABILITIES | {"companion.ambient"},
    )

    async def no_mentions(_session, _reference):
        raise AssertionError("Plain group participation must not resolve a participant")

    async with native_tools() as dispatcher:
        register_companion_dialogue(dispatcher)
        register_send_msg(dispatcher, SendMsgToolContext(no_mentions))
        with (
            companion_participants_scope(registry),
            dialogue_control_scope() as control,
            agent_access_scope(ambient),
            llm_chat_tool_trace_scope(trace),
            llm_chat_delivery_scope(state),
            agno_tool_call_limit_scope(8),
        ):
            selected = requirement(
                "companion_dialogue", {"action": "observe", "engaged_speakers": [alice_id]}, "engaged-alice"
            )
            assert await llm_service.run_llm_tools(RunOutput(requirements=[selected])) is True
            assert json.loads(selected.external_execution_result)["ok"] is True
            assert control.engaged_speakers == (alice_id,)
            assert registry.participation_payload(confirmed=False)["participants"] == []
            assert registry.evidence_members() == ()
            for invalid in ([bob_id, "m_ffffffffff"], [queued_id]):
                rejected = requirement(
                    "companion_dialogue",
                    {"action": "wait", "seconds": 45, "engaged_speakers": invalid},
                    "reject-nonauthor",
                )
                assert await llm_service.run_llm_tools(RunOutput(requirements=[rejected])) is True
                assert json.loads(rejected.external_execution_result)["ok"] is False
                assert control.decision == DialogueDecision()
                assert control.engaged_speakers == (alice_id,)
            send = requirement(
                "send_msg", {"segments": [{"type": "text", "text": "Blue sounds lovely, Alice."}]}, "engaged-send"
            )
            assert (
                await llm_service.run_llm_tools(
                    RunOutput(requirements=[send]), session=alice, ctx=Contexts({ITEM_SESSION: alice})
                )
                is True
            )
            assert json.loads(send.external_execution_result)["ok"] is True
            assert state.confirmed_deliveries == 1
            participation = registry.participation_payload(confirmed=bool(state.confirmed_deliveries))
            assert participation["participants"] == [
                {"user_id": "alice", "speaker_id": alice_id, "source_message_ids": ["engagement-alice"]}
            ]
            assert [identity.user_id for _, identity in registry.evidence_members()] == ["alice"]
            assert trace.events[0].effect == "none"
            assert trace.events[0].outcome["engaged_speakers"] == [alice_id]
    assert len(native_transport.network.wire) == 1
