"""Companion budget deferral through real preparation, image ownership and native tools."""

from __future__ import annotations

from copy import deepcopy
import json
from types import SimpleNamespace
import base64
import asyncio
from datetime import timedelta
from contextlib import asynccontextmanager

import pytest
from arclet.entari import Image
from arclet.letoderea import Contexts
from companion_helpers import NOW, Clock, scope, config, response, tool_call, native_tools, install_provider
from arclet.entari.const import ITEM_SESSION

from plugins.llm_chat import identity as identity_runtime, agent_events, steer_inputs, agent_turn_setup
from plugins.llm_chat.identity import ChatIdentity
from plugins.llm_chat.steering import steering_scope
from plugins.llm_chat.companion import store, dialogue_execution
from plugins.llm_chat.generation import generate_chat_response
from plugins.llm_chat.image_inputs import ImageInputs, ImageInputError
from utils.companion_core.dialogue import DialogueDecision
from plugins.llm_chat.prepared_media import prepare_media
from plugins.llm_chat.tools.send_msg import SendMsgToolContext, register_send_msg
from plugins.llm_chat.context_builder import estimate_tokens
from plugins.llm_chat.tools.finish_turn import register_finish_turn
from plugins.llm_chat.core.media_delivery import build_media_intent
from plugins.llm_chat.companion.participants import CompanionParticipantRegistry, companion_participants_scope
from plugins.llm_chat.companion.dialogue_manager import DialogueInput, DialogueManager, DialogueOutcome
from plugins.llm_chat.companion.dialogue_steering import DialogueSteeringState

pytest_plugins = ["companion_helpers"]
_PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=")


def identity(session):
    return ChatIdentity(str(session.user.id) + "-unified", "Same display name", str(session.user.id) + "-capability")


def incoming(session, *, directed=False, sequence=2, ctx=None):
    return DialogueInput(
        session, ctx if ctx is not None else Contexts(), directed, NOW - timedelta(seconds=3), sequence
    )


@asynccontextmanager
async def prepared_dialogue(
    database, monkeypatch, root, *, initial=(), defer=None, settings=None, root_item=None, attachment_root=None
):
    # Preparation-only cases must never reach a real provider. Native generation
    # cases replace this empty script at their explicit completion boundary.
    install_provider(monkeypatch, [])
    native_scope = await scope(database, channel=root.channel.id)
    await store.set_mode(native_scope.id, "companion", persona_key="alpha", now=NOW)
    settings = settings or config(max_input_tokens=100_000, output_reserve_tokens=0)
    root_item = root_item or incoming(root, directed=True, sequence=1)
    state = DialogueSteeringState(root_item, (root_item, *initial), defer_input=defer)
    for item in initial:
        assert state.admit(item)
    registry = CompanionParticipantRegistry(settings, root, identity(root))
    for item in (root_item, *initial):
        await registry.register_input(
            item.session,
            identity(item.session),
            own_text=item.session.elements.extract_plain_text().strip(),
            directed=item.directed,
            received_at=item.received_at,
        )

    async def resolve(session):
        return identity(session)

    monkeypatch.setattr(steer_inputs, "resolve_chat_identity", resolve)
    monkeypatch.setattr(
        identity_runtime,
        "get_channel_perception",
        lambda: SimpleNamespace(resolve_participant_by_platform_user=no_mentions),
    )
    inputs = ImageInputs(attachment_root=attachment_root)
    inputs.supports_image_input = True
    with steering_scope(state), companion_participants_scope(registry):
        text = root.elements.extract_plain_text().strip()
        prepared = await agent_turn_setup.prepare_agent_turn(
            settings,
            root,
            identity(root),
            model_name="test-model",
            model_text=text,
            raw_user_text=text,
            image_inputs=inputs,
            content=text,
            current_content=None,
            forwarded_messages=(),
            mentioned_participants=(),
            warn=pytest.fail,
            tool_schemas=(),
            is_operator=True,
        )
        try:
            yield state, registry, prepared, settings
        finally:
            await state.aclose()
            await inputs.aclose()


def bind_budget(state, prepared, settings, root, *, slack=1200):
    captured = deepcopy(settings)
    base = estimate_tokens(None, [{"role": "system", "content": prepared.system}, *prepared.chat_messages])
    captured.max_input_tokens = base + slack
    captured.output_reserve_tokens = 0
    steer_inputs.bind_steer_inputs(state, prepared, captured, root, model_name=None, warn=pytest.fail)
    return base


async def generate(root, state, prepared):
    return await generate_chat_response(
        prepared.chat_messages,
        system=prepared.system,
        model="test-model",
        channel_id=root.channel.id,
        ctx=Contexts({ITEM_SESSION: root, "$event": root.event}),
        web_limits=prepared.web_limits,
        delivery_state=prepared.delivery_state,
        image_inputs=prepared.image_inputs,
        media_intent=prepared.media_intent,
        tool_trace=prepared.lifecycle.tool_trace,
        agent_events=prepared.agent_events,
        agent_access=prepared.agent_access,
        resolution=prepared.resolution,
    )


async def no_mentions(_session, _reference):
    raise AssertionError("Budget regression must not resolve an invented mention")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Unrelated first line\nSearch online for a reference image and draw a new image", True),
        ("Find an image online\nUse it as reference to draw a scene", True),
        ("Search online for an image\nDo not draw or generate anything", False),
        ("quoted: Search online for a reference image and draw a new image", False),
    ],
)
def test_media_reference_intent_retains_multiline_and_negation_semantics(text, expected):
    assert build_media_intent(text).requires_web_reference is expected


@pytest.mark.asyncio
async def test_preparation_failure_returns_initial_and_live_sources_without_requeueing_root(
    companion_db,
    native_transport,
    monkeypatch,
):
    native_scope = await scope(companion_db)
    await store.set_mode(native_scope.id, "companion", persona_key="alpha", now=NOW)
    root = native_transport.session(message="preparation-root")
    initial = native_transport.session(user="platform-bob", message="preparation-initial")
    live = native_transport.session(user="platform-carol", message="preparation-live")
    initial_ctx, live_ctx = Contexts(), Contexts()
    entered, release = asyncio.Event(), asyncio.Event()
    original_register = CompanionParticipantRegistry.register_input
    registrations, calls = [], []

    async def resolve(session):
        return identity(session)

    async def fail_root_profile(registry, session, member, **kwargs):
        registrations.append(session)
        if session is root:
            entered.set()
            await release.wait()
            raise RuntimeError("Preparation failed before native input binding")
        return await original_register(registry, session, member, **kwargs)

    monkeypatch.setattr(dialogue_execution, "resolve_chat_identity", resolve)
    monkeypatch.setattr(CompanionParticipantRegistry, "register_input", fail_root_profile)

    async def runner(batch):
        calls.append(batch)
        if len(calls) == 1:
            return await dialogue_execution.run_dialogue_batch(config(), batch, tool_schemas=(), clock=Clock())
        assert {id(item.session) for item in batch.inputs} == {id(initial), id(live)}
        assert next(item for item in batch.inputs if item.session is initial).ctx is initial_ctx
        assert next(item for item in batch.inputs if item.session is live).ctx is live_ctx
        return DialogueOutcome(DialogueDecision("end"))

    clock = Clock()
    manager = DialogueManager(config(), runner, clock=clock, session_factory=companion_db.factory)
    try:
        await manager.admit(root, Contexts(), directed=False)
        await manager.admit(initial, initial_ctx, directed=False)
        clock.advance(seconds=2)
        manager.wake()
        await asyncio.wait_for(entered.wait(), 10)
        await manager.admit(live, live_ctx, directed=True)
        release.set()
        async with asyncio.timeout(10):
            while manager.active(native_scope.id):
                await asyncio.sleep(0.01)
        assert len(calls) == 2
        assert registrations == [root]
        assert native_transport.network.wire == []
    finally:
        release.set()
        await manager.aclose()


@pytest.mark.asyncio
async def test_partially_bound_preparation_records_private_deferred_not_applied_inputs(
    companion_db,
    native_transport,
    monkeypatch,
):
    root = native_transport.session(message="partially-bound-root")
    extra = native_transport.session(user="platform-bob", message="partially-bound-extra")
    source = incoming(extra)
    returned = []
    async with prepared_dialogue(
        companion_db, monkeypatch, root, initial=(source,), defer=lambda session: returned.append(session) is None
    ) as (state, registry, prepared, settings):
        bind_budget(state, prepared, settings, root)
        assert state.prepared is None
        state.defer_unprepared_inputs()
        state.defer_unprepared_inputs()
        await state.aclose()
        assert returned == [extra]
        assert state.input_deferred(state.inputs[1])
        assert not state.input_applied(state.inputs[1])
        checkpoints = [event for event in prepared.agent_events.events if event.event_type == "steer_checkpoint"]
        assert len(checkpoints) == 1
        assert checkpoints[0].status == "deferred"
        assert not checkpoints[0].model_visible
        assert checkpoints[0].payload["reason"] == "preparation_closed_before_application"
        assert registry.evidence_members() == ((root, identity(root)),)


@pytest.mark.asyncio
async def test_initial_overflow_returns_only_unapplied_sources_and_never_credits_their_author(
    companion_db,
    native_transport,
    monkeypatch,
):
    root = native_transport.session(content="Original directed request", message="root-budget")
    overflow = native_transport.session(
        user="platform-bob", content="private queued fact " * 5000, message="queued-budget"
    )
    later = native_transport.session(user="platform-carol", content="Later ambient input", message="later-budget")
    first, second = incoming(overflow), incoming(later, sequence=3)
    returned = []
    async with prepared_dialogue(
        companion_db, monkeypatch, root, initial=(first, second), defer=lambda source: returned.append(source) is None
    ) as (state, registry, prepared, settings):
        bind_budget(state, prepared, settings, root)
        await state.prepare_first_request(prepared)
        assert returned == [overflow, later]
        assert state.inputs[1].session is first.session
        assert state.inputs[1].ctx is first.ctx
        assert not state.pending
        assert state.applied_revision == state.application_revision == 1
        assert not state.input_applied(state.inputs[1])
        assert not state.owns(overflow)
        assert len(prepared.chat_messages) == 1
        assert registry.evidence_members() == ((root, identity(root)),)
        assert prepared.agent_access.user_id == identity(root).user_id
        assert "private queued fact" not in prepared.agent_access.raw_user_text
        events = await agent_events.load_turn_events(prepared.agent_access.turn_id)
        delayed = [event for event in events if event.event_type in {"steer_received", "steer_checkpoint"}]
        assert delayed
        assert all(not event.model_visible for event in delayed)
        checkpoints = [event for event in delayed if event.event_type == "steer_checkpoint"]
        assert [event.status for event in checkpoints] == ["deferred", "deferred"]
        assert [agent_events.load_event_payload(event)["reason"] for event in checkpoints] == [
            "input_budget_exceeded",
            "ordered_after_budget_overflow",
        ]
        assert [item for item in events if item.event_type == "user_input"]
        assert len([item for item in events if item.event_type == "user_input"]) == 1
        assert (
            agent_events.load_event_payload(next(event for event in delayed if event.event_type == "steer_received"))[
                "received_at"
            ]
            == first.received_at.isoformat()
        )
        assert all(event.created_at.tzinfo is None for event in prepared.agent_events.events)


@pytest.mark.asyncio
async def test_normalized_overflow_revokes_its_existing_image_references(
    companion_db, native_transport, monkeypatch, tmp_path
):
    root = native_transport.session(message="image-root")
    extra = native_transport.session(
        user="platform-bob", message="image-overflow", content="queued image request " * 5000
    )
    returned = []
    async with prepared_dialogue(
        companion_db, monkeypatch, root, attachment_root=tmp_path, defer=lambda source: returned.append(source) is None
    ) as (state, registry, prepared, settings):
        bind_budget(state, prepared, settings, root)
        assert state.admit(incoming(extra))
        inputs = prepared.image_inputs
        inputs.admit_input(extra, state=state)

        async def pixels():
            return _PNG

        ref = inputs.register(extra, source="direct", key="queued-pixels", index=1, load=pixels)
        snapshot = await inputs.resolve(extra, ref, purpose="inspect")
        assert snapshot.attachment is not None
        assert await state.drain() == []
        assert returned == [extra]
        assert inputs.input_views() == []
        with pytest.raises(ImageInputError):
            await inputs.resolve(root, ref, purpose="edit")
        with pytest.raises(ImageInputError):
            await inputs.resolve(extra, ref, purpose="inspect")
        assert registry.evidence_members() == ((root, identity(root)),)


@pytest.mark.asyncio
async def test_nonfitting_directed_priority_does_not_defer_an_earlier_fitting_ambient_source(
    companion_db,
    native_transport,
    monkeypatch,
):
    root = native_transport.session(message="priority-root")
    earlier = native_transport.session(user="platform-bob", message="earlier-fitting", content="Earlier small context")
    urgent = native_transport.session(
        user="platform-carol", message="urgent-oversized", content="oversized directed fact " * 5000
    )
    returned = []
    async with prepared_dialogue(
        companion_db,
        monkeypatch,
        root,
        initial=(incoming(earlier), incoming(urgent, directed=True, sequence=3)),
        defer=lambda source: returned.append(source) is None,
    ) as (state, registry, prepared, settings):
        bind_budget(state, prepared, settings, root, slack=3000)
        await state.prepare_first_request(prepared)
        assert returned == [urgent]
        assert state.input_applied(state.inputs[1])
        assert not state.input_applied(state.inputs[2])
        assert state.application_revision == 2
        assert not state.pending
        contents = json.dumps(prepared.chat_messages)
        assert "Earlier small context" in contents
        assert "oversized directed fact" not in contents
        assert {member.user_id for _, member in registry.evidence_members()} == {identity(root).user_id}


@pytest.mark.asyncio
async def test_native_overflow_keeps_candidate_and_prepared_media_without_replaying_generation(
    companion_db,
    native_transport,
    monkeypatch,
):
    root = native_transport.session(message="native-media-root", content="A short original request")
    extra = native_transport.session(
        user="platform-bob", message="native-media-overflow", content="queued supplement " * 5000
    )
    returned, generated = [], []
    async with prepared_dialogue(
        companion_db, monkeypatch, root, defer=lambda source: returned.append(source) is None
    ) as (state, registry, prepared, settings):
        bind_budget(state, prepared, settings, root, slack=3000)
        await state.prepare_first_request(prepared)
        media = {}

        async def prepare_image() -> dict:
            """Prepare exactly one immutable generated image for this native turn."""
            generated.append("once")
            media.update(
                prepare_media(
                    root,
                    Image(src="data:image/png;base64," + base64.b64encode(_PNG).decode()),
                    byte_count=len(_PNG),
                    tool_name="prepare_image",
                )
            )
            return media

        def candidate(_payload):
            assert state.admit(incoming(extra))
            return response(
                calls=[
                    tool_call(
                        "send_msg", {"segments": [{"type": "media", "media_ref": media["media_ref"]}]}, "media-send"
                    )
                ]
            )

        def finish_media(payload):
            assert prepared.delivery_state.confirmed_media_deliveries == 1, payload["messages"]
            return response(calls=[tool_call("finish_turn", {"outcome": "delivered"}, "media-finish")])

        payloads = install_provider(
            monkeypatch,
            [
                response(calls=[tool_call("prepare_image", {}, "prepare-once")]),
                candidate,
                finish_media,
            ],
        )
        async with native_tools() as dispatcher:
            prepare_image.__module__ = dispatcher.plugin.module.__name__
            dispatcher(prepare_image)
            register_send_msg(dispatcher, SendMsgToolContext(no_mentions))
            register_finish_turn(dispatcher)
            await generate(root, state, prepared)
        assert generated == ["once"]
        assert returned == [extra]
        assert len(payloads) == 3
        assert len(native_transport.network.wire) == 1
        assert prepared.delivery_state.confirmed_media_deliveries == 1
        assert state.applied_revision == 1
        assert not state.pending
        assert not any(
            event.event_type == "steer_checkpoint" and event.payload.get("status") == "candidate_replaced"
            for event in prepared.agent_events.events
        )


@pytest.mark.asyncio
async def test_native_fitting_directed_input_behind_overflow_replaces_only_unsent_candidate(
    companion_db,
    native_transport,
    monkeypatch,
):
    root = native_transport.session(message="direct-root", content="Original question")
    ambient = native_transport.session(
        user="platform-bob", message="ambient-overflow", content="queued background " * 5000
    )
    directed = native_transport.session(
        user="platform-carol", message="fitting-directed", content="Correct the answer to blue"
    )
    returned = []
    async with prepared_dialogue(
        companion_db, monkeypatch, root, defer=lambda source: returned.append(source) is None
    ) as (state, registry, prepared, settings):
        bind_budget(state, prepared, settings, root, slack=3000)
        await state.prepare_first_request(prepared)

        def stale(_payload):
            assert state.admit(incoming(ambient))
            assert state.admit(incoming(directed, directed=True, sequence=3))
            return response(
                calls=[tool_call("send_msg", {"segments": [{"type": "text", "text": "Obsolete answer"}]}, "stale-send")]
            )

        payloads = install_provider(
            monkeypatch,
            [
                stale,
                response(
                    calls=[
                        tool_call(
                            "send_msg", {"segments": [{"type": "text", "text": "The answer is blue."}]}, "correct-send"
                        ),
                        tool_call("finish_turn", {"outcome": "delivered"}, "correct-finish"),
                    ]
                ),
            ],
        )
        async with native_tools() as dispatcher:
            register_send_msg(dispatcher, SendMsgToolContext(no_mentions))
            register_finish_turn(dispatcher)
            await generate(root, state, prepared)
        assert returned == [ambient]
        assert len(payloads) == 2
        assert len(native_transport.network.wire) == 1
        assert "Obsolete answer" not in native_transport.network.wire[0]["content"]
        wire = json.dumps(payloads[1]["messages"])
        assert "Correct the answer to blue" in wire
        assert "queued background" not in wire
        assert state.input_applied(state.inputs[2])
        assert not state.input_applied(state.inputs[1])
        assert prepared.agent_access.user_id == identity(root).user_id
        assert "Correct the answer" not in prepared.agent_access.raw_user_text
        applied = [
            event
            for event in prepared.agent_events.events
            if event.event_type == "user_input" and event.payload.get("steer_sequence") == 3
        ]
        assert len(applied) == 1
        assert applied[0].status == "applied"
        assert applied[0].payload["host_author"]["user_id"] == identity(directed).user_id
        assert {member.user_id for _, member in registry.evidence_members()} == {
            identity(root).user_id,
            identity(directed).user_id,
        }


@pytest.mark.asyncio
async def test_native_budget_accounts_for_completed_tool_transcript_before_applying_supplement(
    companion_db,
    native_transport,
    monkeypatch,
):
    root = native_transport.session(message="tool-budget-root", content="Original task")
    extra = native_transport.session(user="platform-bob", message="tool-budget-extra", content="supplement fact " * 500)
    returned, effects = [], []
    result_text = "completed tool fact " * 1000
    async with prepared_dialogue(
        companion_db, monkeypatch, root, defer=lambda source: returned.append(source) is None
    ) as (state, registry, prepared, settings):
        slack = estimate_tokens(None, [{"role": "tool", "content": result_text}]) + 500
        bind_budget(state, prepared, settings, root, slack=slack)
        await state.prepare_first_request(prepared)

        async def committed_result() -> str:
            """Return an already completed operation without replaying its effect."""
            effects.append("once")
            assert state.admit(incoming(extra))
            return result_text

        payloads = install_provider(
            monkeypatch,
            [
                response(calls=[tool_call("committed_result", {}, "committed-result")]),
                response("The completed result is retained."),
            ],
        )
        async with native_tools() as dispatcher:
            committed_result.__module__ = dispatcher.plugin.module.__name__
            dispatcher(committed_result)
            await generate(root, state, prepared)
        assert effects == ["once"]
        assert returned == [extra]
        assert len(payloads) == 2
        assert "completed tool fact" in json.dumps(payloads[1]["messages"])
        assert "supplement fact" not in json.dumps(payloads[1]["messages"])
        assert state.request_budget_snapshot is not None
        assert any(message["role"] == "tool" for message in state.request_budget_snapshot[1])
        assert state.applied_revision == 1
        assert not state.pending


@pytest.mark.asyncio
async def test_oversized_root_and_detached_return_never_claim_successful_deferral(
    companion_db,
    native_transport,
    monkeypatch,
):
    root = native_transport.session(message="oversized-root", content="unbounded original " * 5000)
    returned = []
    async with prepared_dialogue(
        companion_db, monkeypatch, root, defer=lambda source: returned.append(source) is None
    ) as (state, registry, prepared, settings):
        captured = deepcopy(settings)
        captured.max_input_tokens = 2048
        captured.output_reserve_tokens = 1024
        steer_inputs.bind_steer_inputs(state, prepared, captured, root, model_name=None, warn=pytest.fail)
        with pytest.raises(steer_inputs.SteerInputRejected, match="input_budget_exceeded"):
            await state.prepare_first_request(prepared)
        assert returned == []
        assert state.application_revision == 1

    normal_root = native_transport.session(message="detached-root")
    extra = native_transport.session(
        user="platform-bob", message="detached-extra", content="unbounded supplement " * 5000
    )
    async with prepared_dialogue(companion_db, monkeypatch, normal_root, defer=lambda _source: False) as (
        state,
        registry,
        prepared,
        settings,
    ):
        bind_budget(state, prepared, settings, normal_root)
        assert state.admit(incoming(extra))
        with pytest.raises(steer_inputs.SteerInputRejected, match="input_budget_exceeded"):
            await state.drain()
        checkpoints = [event for event in prepared.agent_events.events if event.event_type == "steer_checkpoint"]
        assert checkpoints[-1].status == "refused"
        assert state.pending
        await state.aclose()
        assert not state.defer_input(extra, budget_exceeded=True)


@pytest.mark.asyncio
async def test_real_owner_consumes_oversized_root_refusal_without_automatic_requeue(
    companion_db,
    native_transport,
    monkeypatch,
):
    native_scope = await scope(companion_db)
    await store.set_mode(native_scope.id, "companion", persona_key="alpha", now=NOW)
    source = native_transport.session(message="oversized-owned-root", content="oversized root fact " * 5000)
    calls = []

    async def runner(batch):
        calls.append(batch)
        async with prepared_dialogue(
            companion_db, monkeypatch, source, root_item=batch.inputs[0], defer=batch.defer_input
        ) as (state, registry, prepared, settings):
            batch.bind_admitter(state.admit)
            captured = deepcopy(settings)
            captured.max_input_tokens, captured.output_reserve_tokens = 2048, 1024
            steer_inputs.bind_steer_inputs(state, prepared, captured, source, model_name=None, warn=pytest.fail)
            with pytest.raises(steer_inputs.SteerInputRejected, match="input_budget_exceeded"):
                await state.prepare_first_request(prepared)
        return DialogueOutcome(status="failed")

    manager = DialogueManager(config(), runner, clock=Clock(), session_factory=companion_db.factory)
    try:
        assert await manager.admit(source, Contexts(), directed=True)
        async with asyncio.timeout(10):
            while manager.active(native_scope.id):
                await asyncio.sleep(0.01)
        manager.wake()
        await asyncio.sleep(0.01)
        assert len(calls) == 1
        owner = manager._owners[native_scope.id]
        assert not owner.pending
        assert not owner.deferred
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_real_owner_receives_exact_source_again_after_native_cumulative_overflow(
    companion_db,
    native_transport,
    monkeypatch,
):
    native_scope = await scope(companion_db)
    await store.set_mode(native_scope.id, "companion", persona_key="alpha", now=NOW)
    root = native_transport.session(message="owner-root", content="root fact " * 1500)
    supplement = native_transport.session(user="platform-bob", message="owner-supplement", content="next fact " * 1500)
    original_ctx = Contexts({"$event": supplement.event})
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    seen = []

    async def runner(batch):
        seen.append(batch)
        root_item = batch.inputs[0]
        async with prepared_dialogue(
            companion_db,
            monkeypatch,
            root_item.session,
            root_item=root_item,
            initial=batch.inputs[1:],
            defer=batch.defer_input,
        ) as (state, registry, prepared, settings):
            batch.bind_admitter(state.admit)
            bind_budget(state, prepared, settings, root_item.session, slack=700)
            if len(seen) == 1:
                entered.set()
                await release.wait()
            await state.prepare_first_request(prepared)
            if len(seen) == 1:
                assert state.application_revision == 1
                assert not state.pending
            else:
                assert batch.inputs[0].session is supplement
                assert batch.inputs[0].ctx["$event"] is supplement.event
                assert state.application_revision == 1
                finished.set()
        return DialogueOutcome(DialogueDecision("end"))

    manager = DialogueManager(config(), runner, clock=Clock(), session_factory=companion_db.factory)
    try:
        await manager.admit(root, Contexts(), directed=True)
        await asyncio.wait_for(entered.wait(), 10)
        await manager.admit(supplement, original_ctx, directed=True)
        original_ctx.clear()
        release.set()
        await asyncio.wait_for(finished.wait(), 10)
        assert len(seen) == 2
        assert seen[0].inputs[0].session is root
        assert seen[1].inputs[0].session is supplement
    finally:
        release.set()
        await manager.aclose()
