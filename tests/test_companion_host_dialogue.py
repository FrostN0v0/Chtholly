"""Native host openings, explicit waits and receipt fences through real companion consumers."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from satori import ChannelType
from companion_helpers import (
    NOW,
    MODEL,
    Clock,
    scope,
    config,
    response,
    tool_call,
    native_rows,
    native_tools,
    enable_member,
    install_provider,
)

from utils.companion_core import LifeState, ContactProposal, ReflectionProposal
from plugins.llm_chat.models import AgentTurn, AgentEvent, Conversation, UserRelation, RelationshipEvidence
from plugins.llm_chat.companion import store, host_dialogue
from plugins.llm_chat.generation import generate_chat_response
from utils.companion_core.dialogue import DialogueDecision
from plugins.llm_chat.tools.send_msg import SendMsgToolContext, register_send_msg
from plugins.llm_chat.companion.types import CompanionConflict
from plugins.llm_chat.tools._delivery import send_with_delivery
from plugins.llm_chat.companion.models import CompanionMember, CompanionContact
from plugins.llm_chat.companion.context import companion_limits
from plugins.llm_chat.tools.finish_turn import register_finish_turn
from plugins.llm_chat.companion.claim_store import claim_contact
from plugins.llm_chat.companion.scope_kinds import bind_scope_channel
from plugins.llm_chat.companion.opportunities import build_opportunity, validate_opportunity
from plugins.llm_chat.tools.companion_dialogue import register_companion_dialogue
from plugins.llm_chat.companion.dialogue_control import current_dialogue_control
from plugins.llm_chat.companion.dialogue_manager import DialogueBatch

pytest_plugins = ["companion_helpers"]


async def make_batch(native_scope, native_session, *, trigger="wait", opportunity=None):
    mode = await store.get_mode(native_scope.id)
    callbacks = []

    async def pull(_limit):
        return ()

    async def valid():
        current = await store.get_mode(native_scope.id)
        return (
            current.mode == "companion"
            and current.revision == mode.revision
            and current.persona_key == mode.persona_key
        )

    return DialogueBatch(
        native_scope, mode, (), opportunity, trigger, native_session, None, pull, valid, callbacks.append
    ), callbacks


async def committed_opportunity(native_scope, proposal):
    claim = await store.claim_review(native_scope.id, "alpha", now=NOW)
    assert claim is not None
    assert await store.finish_review(
        claim, ReflectionProposal(life=LifeState(), contact=proposal), now=NOW, next_review_at=NOW + timedelta(hours=1)
    )
    return build_opportunity(claim, proposal)


async def no_mentions(_session, _reference):
    raise AssertionError("Plain host opening must not resolve an invented participant")


@pytest.mark.asyncio
async def test_native_channel_opportunity_uses_full_bridge_stages_wait_and_persists_confirmed_output(
    companion_db,
    native_transport,
    monkeypatch,
):
    native_scope = await scope(companion_db)
    member = await enable_member(companion_db, native_scope)
    inbound = native_transport.session()
    await bind_scope_channel(native_scope, inbound.channel)
    proposal = ContactProposal(
        target="channel",
        key="group-palette",
        purpose="share",
        topic="An imagined indigo palette",
        reason="A concrete group conversation topic",
    )
    opportunity = await committed_opportunity(native_scope, proposal)
    batch, _ = await make_batch(native_scope, inbound, trigger="opportunity", opportunity=opportunity)
    payloads = install_provider(
        monkeypatch,
        [
            response(
                calls=[
                    tool_call("companion_dialogue", {"action": "wait", "seconds": 45}, "host-wait"),
                    tool_call(
                        "send_msg",
                        {"segments": [{"type": "text", "text": "I imagined a tiny indigo moon."}]},
                        "host-send",
                    ),
                ]
            ),
            response(calls=[tool_call("finish_turn", {"outcome": "delivered"}, "host-finish")]),
        ],
    )
    monkeypatch.setattr(host_dialogue, "get_model_config", lambda *_args, **_kwargs: MODEL)

    async def native_runner(session, ctx, prepared, **kwargs):
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
        register_companion_dialogue(dispatcher)
        register_send_msg(dispatcher, SendMsgToolContext(no_mentions))
        register_finish_turn(dispatcher)
        outcome = await host_dialogue.run_host_dialogue(
            config(), batch, tool_schemas=(), clock=Clock(), runner=native_runner
        )
    assert outcome.status == "completed"
    assert outcome.confirmed_deliveries == 1
    assert outcome.decision == DialogueDecision("wait", 45)
    assert len(payloads) == 2
    assert len(native_transport.network.wire) == 1
    attempts = await native_rows(companion_db, CompanionContact)
    assert len(attempts) == 1
    assert attempts[0].member_ref == attempts[0].user_id == attempts[0].platform_user_id == ""
    assert attempts[0].status == "delivered"
    assert json.loads(attempts[0].confirmed_receipts_json) == ["native-receipt-1"]
    turns = [turn for turn in await native_rows(companion_db, AgentTurn) if turn.trigger_kind == "autonomous"]
    assert len(turns) == 1
    assert turns[0].user_id == ""
    assert turns[0].trigger_message_id == ""
    events = [event for event in await native_rows(companion_db, AgentEvent) if event.turn_id == turns[0].id]
    assert not any(event.event_type == "user_input" for event in events)
    assert any(event.event_type == "companion_trigger" for event in events)
    assert [row.content for row in await native_rows(companion_db, Conversation) if row.role == "assistant"] == [
        "I imagined a tiny indigo moon."
    ]
    assert {row.user_id for row in await native_rows(companion_db, CompanionMember)} == {member.user_id}
    assert all(row.user_id for row in await native_rows(companion_db, UserRelation))
    assert await native_rows(companion_db, RelationshipEvidence) == []


@pytest.mark.asyncio
async def test_silent_waits_consume_finite_daily_budget_without_transport_or_fake_human(
    companion_db,
    native_transport,
    monkeypatch,
):
    native_scope = await scope(companion_db)
    await enable_member(companion_db, native_scope)
    inbound = native_transport.session()
    monkeypatch.setattr(host_dialogue, "get_model_config", lambda *_args, **_kwargs: MODEL)
    seen = []

    async def silent(session, _ctx, prepared, **_kwargs):
        seen.append(prepared)
        assert session.event.message is None
        assert session.user.id == native_transport.account.self_id
        assert prepared.agent_access.user_id == prepared.agent_access.raw_user_text == ""
        assert not prepared.agent_access.is_operator
        current_dialogue_control().stage("wait", 30)
        return "silent"

    settings = config(companion_daily_contacts=1)
    first, _ = await make_batch(native_scope, inbound)
    outcome = await host_dialogue.run_host_dialogue(settings, first, tool_schemas=(), clock=Clock(), runner=silent)
    assert outcome.decision == DialogueDecision("wait", 30)
    assert outcome.status == "completed"
    second, _ = await make_batch(native_scope, inbound)
    blocked = await host_dialogue.run_host_dialogue(settings, second, tool_schemas=(), clock=Clock(), runner=silent)
    assert blocked.decision.action == "end"
    assert len(seen) == 1
    assert native_transport.network.wire == []
    attempts = await native_rows(companion_db, CompanionContact)
    assert len(attempts) == 1
    assert attempts[0].origin_review_token == ""
    assert attempts[0].status == "silent"


@pytest.mark.asyncio
async def test_private_wait_targets_only_known_peer_and_quiet_hours_consume_the_wake(
    companion_db,
    native_transport,
    monkeypatch,
):
    native_scope = await scope(companion_db, channel="private:alice")
    peer = await enable_member(companion_db, native_scope)
    inbound = native_transport.session(channel=native_scope.channel_id, channel_type=ChannelType.DIRECT)
    monkeypatch.setattr(host_dialogue, "get_model_config", lambda *_args, **_kwargs: MODEL)
    seen = []

    async def silent(_session, _ctx, prepared, **_kwargs):
        seen.append(prepared.agent_access.user_id)
        current_dialogue_control().stage("observe", 30)
        return "silent"

    first, _ = await make_batch(native_scope, inbound)
    assert (
        await host_dialogue.run_host_dialogue(config(), first, tool_schemas=(), clock=Clock(), runner=silent)
    ).status == "completed"
    assert seen == [peer.user_id]
    quiet, _ = await make_batch(native_scope, inbound)
    blocked = await host_dialogue.run_host_dialogue(
        config(), quiet, tool_schemas=(), clock=Clock(NOW.replace(hour=16)), runner=silent
    )
    assert blocked.decision.action == "end"
    assert seen == [peer.user_id]
    proposal = ContactProposal(
        target="channel", key="not-a-private-target", purpose="free_chat", topic="A thought", reason="A topic"
    )
    review = await store.claim_review(native_scope.id, "alpha", now=NOW)
    with pytest.raises(CompanionConflict, match="native text channel"):
        await store.finish_review(
            review,
            ReflectionProposal(life=LifeState(), contact=proposal),
            now=NOW,
            next_review_at=NOW + timedelta(hours=1),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("transport_outcome", ["unknown", "empty"])
async def test_unknown_host_send_is_never_rescheduled_or_replayed(
    companion_db,
    native_transport,
    monkeypatch,
    transport_outcome,
):
    native_scope = await scope(companion_db)
    await enable_member(companion_db, native_scope)
    inbound = native_transport.session()
    monkeypatch.setattr(host_dialogue, "get_model_config", lambda *_args, **_kwargs: MODEL)
    native_transport.network.outcome = transport_outcome
    invocations = []

    async def send(session, _ctx, prepared, **_kwargs):
        invocations.append(prepared.agent_access.turn_id)
        current_dialogue_control().stage("wait", 30)
        await send_with_delivery(
            session, "One irreversible host send", prepared.delivery_state, texts=["One irreversible host send"]
        )
        return "completed"

    batch, _ = await make_batch(native_scope, inbound)
    outcome = await host_dialogue.run_host_dialogue(config(), batch, tool_schemas=(), clock=Clock(), runner=send)
    assert outcome.status == "unknown"
    assert outcome.decision.action == "end"
    assert outcome.confirmed_deliveries == 0
    again, _ = await make_batch(native_scope, inbound)
    blocked = await host_dialogue.run_host_dialogue(config(), again, tool_schemas=(), clock=Clock(), runner=send)
    assert blocked.decision.action == "end"
    assert len(invocations) == len(native_transport.network.wire) == 1
    row = (await native_rows(companion_db, CompanionContact))[0]
    assert row.status == "unknown"
    assert row.transport_inflight


@pytest.mark.asyncio
async def test_opportunity_handoff_revalidates_the_committed_motive_and_scope(companion_db, native_transport):
    native_scope = await scope(companion_db)
    peer = await enable_member(companion_db, native_scope)
    await bind_scope_channel(native_scope, native_transport.session().channel)
    proposal = ContactProposal(
        member_ref=peer.member_ref,
        key="honest-motive",
        purpose="share",
        topic="An imagined color",
        reason="A shared topic",
    )
    payload = await committed_opportunity(native_scope, proposal)
    review, restored = validate_opportunity(payload, scope_id=native_scope.id)
    assert restored == proposal
    with pytest.raises(ValueError, match="different scope"):
        validate_opportunity(payload, scope_id=native_scope.id + 1)
    forged = proposal.model_copy(update={"topic": "A different uncommitted motive"})
    with pytest.raises(CompanionConflict, match="committed originating review"):
        await claim_contact(
            native_scope.id, "alpha", peer.member_ref, forged, now=NOW, origin=review, limits=companion_limits(config())
        )
