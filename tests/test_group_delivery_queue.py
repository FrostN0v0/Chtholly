"""Deterministic concurrency and fairness contracts for ready-only delivery."""

from __future__ import annotations

import asyncio

import pytest

from utils.group_delivery_core import DeliveryQueue


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleepers: list[tuple[float, asyncio.Future[None]]] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        future = asyncio.get_running_loop().create_future()
        self.sleepers.append((self.now + seconds, future))
        try:
            await future
        finally:
            self.sleepers = [(due, item) for due, item in self.sleepers if item is not future]

    def advance(self, seconds: float) -> None:
        self.now += seconds
        for due, future in self.sleepers:
            if due <= self.now and not future.done():
                future.set_result(None)


async def settle() -> None:
    for _ in range(16):
        await asyncio.sleep(0)


async def test_ready_bursts_rotate_whole_turns_without_starving_a_third_participant() -> None:
    clock = Clock()
    queue = DeliveryQueue(clock=clock, sleep=clock.sleep)
    a, b, c = object(), object(), object()
    sent = []

    async def send(turn: object, text: str) -> None:
        async with queue.slot(turn, interval=1.0) as permit:
            sent.append((text, permit.reply))

    await send(a, "a1")
    pending = [
        asyncio.create_task(send(turn, text))
        for turn, text in ((a, "a2"), (a, "a3"), (a, "a4"), (b, "b1"), (b, "b2"), (b, "b3"), (c, "c1"))
    ]
    try:
        await settle()
        for _ in pending:
            clock.advance(1.0)
            await settle()
        await asyncio.wait_for(asyncio.gather(*pending), timeout=1)
        assert sent == [
            ("a1", True),
            ("a2", False),
            ("a3", False),
            ("b1", True),
            ("b2", False),
            ("b3", False),
            ("c1", True),
            ("a4", True),
        ]
    finally:
        queue.close()
        await asyncio.gather(*pending, return_exceptions=True)
        await queue.wait_closed()


async def test_turn_switch_requotes_but_standalone_fourth_message_does_not() -> None:
    queue = DeliveryQueue()
    a, b = object(), object()
    replies = []
    for turn in (a, b, b, b, b):
        async with queue.slot(turn, interval=0) as permit:
            replies.append(permit.reply)
    assert replies == [True, True, False, False, False]
    queue.close()
    await queue.wait_closed()


async def test_slow_continuation_cannot_reserve_the_channel_ahead_of_a_ready_user() -> None:
    clock = Clock()
    queue = DeliveryQueue(clock=clock, sleep=clock.sleep)
    sent = []

    async def send(turn: object, text: str, due: float) -> None:
        async with queue.slot(turn, not_before=due, interval=0):
            sent.append(text)

    slow = asyncio.create_task(send(object(), "slow", 60.0))
    try:
        await settle()
        assert clock.sleepers
        await asyncio.wait_for(send(object(), "ready", 0.0), timeout=1)
        assert sent == ["ready"]
        assert not slow.done()
        assert clock.now == 0.0
    finally:
        slow.cancel()
        await asyncio.gather(slow, return_exceptions=True)
        queue.close()
        await queue.wait_closed()
    assert not clock.sleepers


async def test_time_quantum_starts_at_first_transport_not_at_its_completion() -> None:
    clock = Clock()
    queue = DeliveryQueue(clock=clock, sleep=clock.sleep, max_messages=10)
    a, b = object(), object()
    sent = []

    async def send(turn: object, text: str) -> None:
        async with queue.slot(turn, interval=0) as permit:
            sent.append((text, permit.reply))

    async with queue.slot(a, interval=0):
        pending = [asyncio.create_task(send(a, "a2")), asyncio.create_task(send(b, "b1"))]
        await settle()
        clock.advance(5.0)
    await asyncio.wait_for(asyncio.gather(*pending), timeout=1)
    assert sent == [("b1", True), ("a2", True)]
    queue.close()
    await queue.wait_closed()


async def test_interruption_requires_a_fresh_quote_but_elapsed_time_does_not() -> None:
    clock = Clock()
    queue = DeliveryQueue(clock=clock, sleep=clock.sleep, max_group_seconds=100)
    turn = object()
    async with queue.slot(turn, interval=0):
        queue.interrupt()
    async with queue.slot(turn, interval=0) as interrupted:
        assert interrupted.reply
    async with queue.slot(turn, interval=0) as continuous:
        assert not continuous.reply
    clock.advance(8.0)
    async with queue.slot(turn, interval=0) as resumed:
        assert not resumed.reply
    queue.close()
    await queue.wait_closed()


async def test_cancelled_queued_or_granted_waiter_never_enters_transport() -> None:
    queue = DeliveryQueue()
    a, b = object(), object()
    sent = []

    async def send_b() -> None:
        async with queue.slot(b, interval=0):
            sent.append("b")

    async with queue.slot(a, interval=0):
        cancelled = asyncio.create_task(send_b())
        await settle()
        queue.cancel_turn(b)
        with pytest.raises(asyncio.CancelledError):
            await cancelled
    async with queue.slot(a, interval=0):
        granted = asyncio.create_task(send_b())
        await settle()
    asyncio.get_running_loop().call_soon(granted.cancel)
    with pytest.raises(asyncio.CancelledError):
        await granted
    assert sent == []
    async with queue.slot(a, interval=0) as permit:
        assert not permit.reply
    queue.close()
    await queue.wait_closed()


async def test_unknown_outcome_is_not_replayed_and_next_send_is_reanchored() -> None:
    queue = DeliveryQueue()
    turn = object()
    attempts = []

    async def fail_transport() -> None:
        async with queue.slot(turn, interval=0):
            attempts.append("unknown")
            raise TimeoutError

    with pytest.raises(TimeoutError):
        await fail_transport()
    async with queue.slot(turn, interval=0) as next_send:
        attempts.append("next")
        assert next_send.reply
    assert attempts == ["unknown", "next"]
    queue.close()
    await queue.wait_closed()


async def test_channels_are_independent_and_close_cancels_holder_waiters_and_timers() -> None:
    clock = Clock()
    active = DeliveryQueue()
    paced = DeliveryQueue(clock=clock, sleep=clock.sleep)
    started = asyncio.Event()
    held_cancelled = asyncio.Event()

    async def hold() -> None:
        async with active.slot(object(), interval=0):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                held_cancelled.set()

    async def wait_for_slot(queue: DeliveryQueue, due: float) -> None:
        async with queue.slot(object(), not_before=due, interval=0):
            raise AssertionError("Closed waiting transport must never start")

    holder = asyncio.create_task(hold())
    await started.wait()
    async with paced.slot(object(), interval=0):
        assert not holder.done()
    waiters = [asyncio.create_task(wait_for_slot(active, 0)), asyncio.create_task(wait_for_slot(paced, 60))]
    await settle()
    active.close()
    paced.close()
    results = await asyncio.gather(holder, *waiters, return_exceptions=True)
    await asyncio.gather(active.wait_closed(), paced.wait_closed())
    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    assert held_cancelled.is_set()
    assert active.idle
    assert paced.idle
    assert not clock.sleepers
    with pytest.raises(RuntimeError):
        async with active.slot(object()):
            raise AssertionError("Disposed queue must reject new sends")
