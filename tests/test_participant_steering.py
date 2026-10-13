"""Behavioral concurrency contracts for cooperative participant admission."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast
import asyncio
from collections.abc import AsyncIterator

import pytest
from arclet.entari import Session
import pytest_asyncio
from arclet.letoderea import BLOCK, Contexts

from plugins.llm_chat import channel_turns
from plugins.llm_chat.steering import (
    MAX_STEER_INPUTS,
    SteerInput,
    SteerPending,
    SteeringState,
    steering_scope,
    current_steering,
    check_steer_boundary,
)
from plugins.llm_chat.core.types import ChatMessage
from plugins.llm_chat.core.delivery import DeliveryRejected


def _session(message_id: str | None, text: str = "same text", **scope: str) -> Session:
    return cast(
        Session,
        SimpleNamespace(
            account=SimpleNamespace(platform=scope.get("platform", "test"), self_id=scope.get("account", "bot")),
            channel=SimpleNamespace(id=scope.get("channel", "group")),
            user=SimpleNamespace(id=scope.get("user", "member")),
            event=SimpleNamespace(message=SimpleNamespace(id=message_id)),
            elements=SimpleNamespace(extract_plain_text=lambda: text),
        ),
    )


async def _message(entry: SteerInput) -> ChatMessage:
    return {"role": "user", "content": entry.session.elements.extract_plain_text()}


@pytest_asyncio.fixture(autouse=True)
async def _settle_participants() -> AsyncIterator[None]:
    yield
    await channel_turns.cancel_active_participant_turns()


@pytest.mark.asyncio
async def test_protocol_ids_deduplicate_but_identical_text_and_missing_ids_do_not() -> None:
    root = _session("root")
    state = SteeringState(root, Contexts())
    second = _session("second")
    missing_first, missing_second = _session(None), _session(None)
    receipts: list[int] = []
    state.on_receive = lambda entry: receipts.append(entry.sequence)
    state.processor = _message

    assert state.receive(second, Contexts())
    assert state.receive(_session("second", "different retransmission text"), Contexts())
    assert state.receive(_session("third"), Contexts())
    assert state.receive(missing_first, Contexts())
    assert state.receive(missing_first, Contexts())
    assert state.receive(missing_second, Contexts())
    assert receipts == [2, 3, 4, 5]
    assert state.owns(root)
    assert state.owns(second)
    assert not state.owns(_session("second"))
    assert not state.receive(_session("foreign", user="other"), Contexts())
    assert [message["content"] for message in await state.drain()] == ["same text"] * 4
    assert state.applied_revision == state.revision == 5
    assert not state.pending
    await state.aclose()


@pytest.mark.asyncio
async def test_serial_drains_include_arrivals_during_processing_and_never_repeat() -> None:
    state = SteeringState(_session("root", "original"), Contexts())
    entered, release = asyncio.Event(), asyncio.Event()
    processed: list[int] = []

    async def process(entry: SteerInput) -> ChatMessage:
        processed.append(entry.sequence)
        if entry.sequence == 2:
            entered.set()
            await release.wait()
        return await _message(entry)

    state.processor = process
    assert state.receive(_session("second", "first supplement"), Contexts())
    first = asyncio.create_task(state.drain())
    await entered.wait()
    assert state.applied_revision == 1
    assert state.receive(_session("third", "second supplement"), Contexts())
    second = asyncio.create_task(state.drain())
    release.set()
    messages, empty = await asyncio.gather(first, second)
    assert [message["content"] for message in messages] == ["first supplement", "second supplement"]
    assert empty == []
    assert processed == [2, 3]
    assert [entry.session.elements.extract_plain_text() for entry in state.inputs] == [
        "original",
        "first supplement",
        "second supplement",
    ]
    await state.aclose()


@pytest.mark.asyncio
async def test_failed_processing_retains_pending_audit_and_does_not_replay() -> None:
    state = SteeringState(_session("root"), Contexts())
    processed: list[int] = []
    unapplied: list[int] = []

    async def process(entry: SteerInput) -> ChatMessage:
        processed.append(entry.sequence)
        if entry.sequence == 3:
            raise ValueError("normalization failed")
        return await _message(entry)

    async def checkpoint() -> None:
        unapplied.extend(entry.sequence for entry in state.inputs if entry.sequence > state.applied_revision)

    state.processor, state.on_close = process, checkpoint
    state.receive(_session("second"), Contexts())
    state.receive(_session("third"), Contexts())
    with pytest.raises(ValueError, match="normalization failed"):
        await state.drain()
    assert state.applied_revision == 2
    with pytest.raises(RuntimeError, match="processing failed"):
        await state.drain()
    assert processed == [2, 3]
    await state.aclose()
    assert unapplied == [3]


@pytest.mark.asyncio
async def test_receipt_must_be_durable_before_processing_and_handler_acknowledgment() -> None:
    entered, release, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    receipts: list[int] = []
    processed: list[int] = []
    state_ready = asyncio.Event()
    state: SteeringState | None = None

    async def handler(_session: Session, _ctx: Contexts) -> object:
        nonlocal state
        state = current_steering()
        assert state is not None
        state.on_receive = lambda entry: receipts.append(entry.sequence)

        async def flush() -> bool:
            if receipts:
                entered.set()
                await release.wait()
            return True

        async def process(entry: SteerInput) -> ChatMessage:
            assert release.is_set()
            processed.append(entry.sequence)
            return await _message(entry)

        state.on_flush, state.processor = flush, process
        state_ready.set()
        await finish.wait()
        return await state.drain()

    wrapped = channel_turns.steer_participant_turn(handler)
    root = asyncio.create_task(wrapped(_session("root"), Contexts()))
    await state_ready.wait()
    supplement = asyncio.create_task(wrapped(_session("second"), Contexts()))
    await entered.wait()
    assert receipts == [2]
    assert not supplement.done()
    assert state is not None
    drain = asyncio.create_task(state.drain())
    await asyncio.sleep(0)
    assert processed == []
    release.set()
    assert await supplement is BLOCK
    assert len(await drain) == 1
    finish.set()
    assert await root == []
    assert processed == [2]


@pytest.mark.asyncio
async def test_failed_receipt_flush_never_executes_input_processor() -> None:
    state = SteeringState(_session("root"), Contexts())
    processed: list[int] = []

    async def unavailable() -> bool:
        return False

    async def process(entry: SteerInput) -> ChatMessage:
        processed.append(entry.sequence)
        return await _message(entry)

    state.on_flush, state.processor = unavailable, process
    state.receive(_session("second"), Contexts())
    with pytest.raises(RuntimeError, match="receipt could not be persisted"):
        await state.drain()
    assert processed == []
    assert state.applied_revision == 1
    assert state.pending
    state.on_flush = None
    await state.aclose()


@pytest.mark.asyncio
async def test_early_arrival_is_retained_until_root_binds_and_root_is_not_cancelled() -> None:
    started, bind = asyncio.Event(), asyncio.Event()
    cancelled: list[bool] = []
    calls: list[str] = []

    async def handler(session: Session, _ctx: Contexts) -> object:
        calls.append(session.event.message.id)
        state = current_steering()
        assert state is not None
        started.set()
        try:
            await bind.wait()
            state.processor = _message
            return [session.elements.extract_plain_text(), *[message["content"] for message in await state.drain()]]
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    wrapped = channel_turns.steer_participant_turn(handler)
    root = asyncio.create_task(wrapped(_session("root", "original"), Contexts()))
    await started.wait()
    assert await wrapped(_session("second", "supplement"), Contexts()) is BLOCK
    assert await wrapped(_session("second", "duplicate"), Contexts()) is BLOCK
    assert not root.done()
    bind.set()
    assert await root == ["original", "supplement"]
    assert calls == ["root"]
    assert cancelled == []


@pytest.mark.asyncio
@pytest.mark.parametrize("dimension", ["platform", "account", "channel", "user"])
async def test_each_full_scope_dimension_preserves_parallel_owners(dimension: str) -> None:
    started = {"first": asyncio.Event(), "second": asyncio.Event()}
    release = asyncio.Event()
    scopes: list[SteeringState] = []

    async def handler(session: Session, _ctx: Contexts) -> object:
        state = current_steering()
        assert state is not None
        scopes.append(state)
        started[session.event.message.id].set()
        await release.wait()
        return session.event.message.id

    wrapped = channel_turns.steer_participant_turn(handler)
    first = asyncio.create_task(wrapped(_session("first"), Contexts()))
    await started["first"].wait()
    second = asyncio.create_task(wrapped(_session("second", **{dimension: "different"}), Contexts()))
    try:
        await asyncio.wait_for(started["second"].wait(), timeout=1)
        assert not first.done()
        assert not second.done()
        assert scopes[0] is not scopes[1]
        assert not scopes[0].owns(scopes[1].inputs[0].session)
    finally:
        release.set()
        results = await asyncio.gather(first, second)
    assert results == ["first", "second"]


@pytest.mark.asyncio
async def test_full_queue_overflow_starts_next_turn_fifo_without_cross_generation_resources() -> None:
    gates = [asyncio.Event(), asyncio.Event()]
    started = [asyncio.Event(), asyncio.Event()]
    states: list[SteeringState] = []

    async def handler(_session: Session, _ctx: Contexts) -> object:
        state = current_steering()
        assert state is not None
        index = len(states)
        states.append(state)
        state.processor = _message
        started[index].set()
        await gates[index].wait()
        await state.drain()
        return [entry.session.event.message.id for entry in state.inputs]

    wrapped = channel_turns.steer_participant_turn(handler)
    root_session = _session("root")
    root = asyncio.create_task(wrapped(root_session, Contexts()))
    await started[0].wait()
    for index in range(MAX_STEER_INPUTS - 1):
        assert await wrapped(_session(f"accepted-{index}"), Contexts()) is BLOCK
    assert await wrapped(_session("accepted-0"), Contexts()) is BLOCK
    assert states[0].revision == MAX_STEER_INPUTS
    next_session = _session("next-root")
    overflow = asyncio.create_task(wrapped(next_session, Contexts()))
    await asyncio.sleep(0)
    after_overflow = asyncio.create_task(wrapped(_session("next-supplement"), Contexts()))
    await asyncio.sleep(0)
    assert not overflow.done()
    assert not after_overflow.done()
    assert not states[0].owns(next_session)
    gates[0].set()
    first_inputs = await root
    await started[1].wait()
    assert await after_overflow is BLOCK
    assert states[0].closed
    assert not states[1].owns(root_session)
    assert states[1].applied_revision == 1
    gates[1].set()
    assert await overflow == ["next-root", "next-supplement"]
    assert first_inputs == ["root", *[f"accepted-{index}" for index in range(MAX_STEER_INPUTS - 1)]]


@pytest.mark.asyncio
async def test_seal_race_keeps_new_arrivals_for_next_turn_in_fifo_order() -> None:
    sealed, finish, next_started, next_finish = (asyncio.Event() for _ in range(4))
    states: list[SteeringState] = []

    async def handler(_session: Session, _ctx: Contexts) -> object:
        state = current_steering()
        assert state is not None
        states.append(state)
        state.processor = _message
        if len(states) == 1:
            assert state.seal()
            sealed.set()
            await finish.wait()
        else:
            next_started.set()
            await next_finish.wait()
            await state.drain()
        return [entry.session.event.message.id for entry in state.inputs]

    wrapped = channel_turns.steer_participant_turn(handler)
    root = asyncio.create_task(wrapped(_session("root"), Contexts()))
    await sealed.wait()
    first = asyncio.create_task(wrapped(_session("new-root"), Contexts()))
    await asyncio.sleep(0)
    second = asyncio.create_task(wrapped(_session("new-supplement"), Contexts()))
    await asyncio.sleep(0)
    assert len(states) == 1
    assert not first.done()
    assert not second.done()
    finish.set()
    assert await root == ["root"]
    await next_started.wait()
    assert await second is BLOCK
    next_finish.set()
    assert await first == ["new-root", "new-supplement"]


@pytest.mark.asyncio
async def test_arrival_during_owner_final_cleanup_waits_for_clean_next_generation() -> None:
    closing, finish_close = asyncio.Event(), asyncio.Event()
    states: list[SteeringState] = []

    async def handler(_session: Session, _ctx: Contexts) -> object:
        state = current_steering()
        assert state is not None
        states.append(state)
        if len(states) == 1:

            async def close() -> None:
                closing.set()
                await finish_close.wait()

            state.on_close = close
        return state.inputs[0].session.event.message.id

    wrapped = channel_turns.steer_participant_turn(handler)
    root = asyncio.create_task(wrapped(_session("root"), Contexts()))
    await closing.wait()
    next_session = _session("next")
    next_turn = asyncio.create_task(wrapped(next_session, Contexts()))
    await asyncio.sleep(0)
    assert states[0].closed
    assert not states[0].owns(next_session)
    assert len(states) == 1
    assert not next_turn.done()
    finish_close.set()
    assert await asyncio.gather(root, next_turn) == ["root", "next"]
    assert states[0] is not states[1]


@pytest.mark.asyncio
async def test_pending_boundary_and_atomic_seal_share_state_with_inherited_children() -> None:
    state = SteeringState(_session("root"), Contexts())
    state.processor = _message
    gate = asyncio.Event()

    async def child() -> None:
        assert current_steering() is state
        await gate.wait()
        check_steer_boundary()

    with steering_scope(state):
        check_steer_boundary()
        inherited = asyncio.create_task(child())
        state.receive(_session("second"), Contexts())
        with pytest.raises(SteerPending):
            check_steer_boundary()
        assert issubclass(SteerPending, DeliveryRejected)
        assert not state.seal()
        await state.drain()
        assert state.seal()
        assert not state.receive(_session("third"), Contexts())
    assert current_steering() is None
    check_steer_boundary()
    await state.aclose()
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await inherited


@pytest.mark.asyncio
async def test_cancelled_overflow_waiter_does_not_cancel_owner_or_remaining_input() -> None:
    started, release = asyncio.Event(), asyncio.Event()
    calls: list[str] = []

    async def handler(session: Session, _ctx: Contexts) -> object:
        calls.append(session.event.message.id)
        if len(calls) == 1:
            state = current_steering()
            assert state is not None
            assert state.seal()
            started.set()
            await release.wait()
        return session.event.message.id

    wrapped = channel_turns.steer_participant_turn(handler)
    root = asyncio.create_task(wrapped(_session("root"), Contexts()))
    await started.wait()
    abandoned = asyncio.create_task(wrapped(_session("abandoned"), Contexts()))
    await asyncio.sleep(0)
    retained = asyncio.create_task(wrapped(_session("retained"), Contexts()))
    await asyncio.sleep(0)
    abandoned.cancel()
    with pytest.raises(asyncio.CancelledError):
        await abandoned
    assert not root.done()
    release.set()
    assert await asyncio.gather(root, retained) == ["root", "retained"]
    assert calls == ["root", "retained"]


@pytest.mark.asyncio
async def test_unload_cancels_and_awaits_owners_and_queued_inputs_without_restart() -> None:
    started, cleaning, release_cleanup = (asyncio.Event() for _ in range(3))
    calls: list[str] = []
    states: list[SteeringState] = []

    async def handler(session: Session, _ctx: Contexts) -> object:
        calls.append(session.event.message.id)
        state = current_steering()
        assert state is not None
        assert state.seal()
        states.append(state)
        started.set()
        try:
            await asyncio.Future()
        finally:
            cleaning.set()
            await release_cleanup.wait()

    wrapped = channel_turns.steer_participant_turn(handler)
    root = asyncio.create_task(wrapped(_session("root"), Contexts()))
    await started.wait()
    queued = asyncio.create_task(wrapped(_session("queued"), Contexts()))
    await asyncio.sleep(0)
    unloading = asyncio.create_task(channel_turns.cancel_active_participant_turns())
    await cleaning.wait()
    assert states[0].closed
    assert not unloading.done()
    release_cleanup.set()
    await unloading
    results = await asyncio.gather(root, queued, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    assert calls == ["root"]
    assert channel_turns._ACTIVE_PARTICIPANT_TURNS == {}
    assert channel_turns._PARTICIPANT_ADMISSIONS == {}
