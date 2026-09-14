"""Import-safe, ready-only group send scheduling in the caller's task."""

from __future__ import annotations

import time
import asyncio
from contextlib import suppress, asynccontextmanager
from collections import deque
from dataclasses import dataclass
from collections.abc import Callable, Awaitable, AsyncIterator


@dataclass(frozen=True, slots=True)
class DeliveryPermit:
    reply: bool


@dataclass(slots=True)
class _Waiter:
    turn: object
    not_before: float
    interval: float
    owner: asyncio.Task[object]
    future: asyncio.Future[None]
    revision: int = 0


class DeliveryQueue:
    """One channel's fair bursts; payload preparation never belongs to this queue."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_messages: int = 3,
        max_group_seconds: float = 4.0,
        reply_after: float = 8.0,
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self._max_messages = max_messages
        self._max_seconds = max_group_seconds
        self._reply_after = reply_after
        self._waiters: deque[_Waiter] = deque()
        self._pump: asyncio.Task[None] | None = None
        self._timer: asyncio.Future[None] | None = None
        self._wake: asyncio.Future[None] | None = None
        self._holder: _Waiter | None = None
        self._closed = False
        self._last_send: float | None = None
        self._last_turn: object | None = None
        self._group_started: float | None = None
        self._group_count = 0
        self._revision = 0
        self._force_reply = True

    @property
    def idle(self) -> bool:
        return not self._waiters and self._holder is None and self._pump is None and self._timer is None

    def _signal(self) -> None:
        if self._wake is not None and not self._wake.done():
            self._wake.set_result(None)
        if not self._closed and self._holder is None and self._pump is None and self._waiters:

            def settled(task: asyncio.Task[None]) -> None:
                if self._pump is task:
                    self._pump = None
                self._signal()

            self._pump = asyncio.create_task(self._run())
            self._pump.add_done_callback(settled)

    def _due(self, waiter: _Waiter) -> float:
        spacing = 0.0 if self._last_send is None else self._last_send + waiter.interval
        return max(waiter.not_before, spacing)

    def _boundary(self, now: float) -> bool:
        return (
            self._group_started is None
            or self._group_count >= self._max_messages
            or now - self._group_started >= self._max_seconds
            or (self._last_send is not None and now - self._last_send >= self._reply_after)
        )

    def _choose(self, now: float) -> _Waiter | None:
        first = same = other = None
        for waiter in self._waiters:
            if waiter.future.cancelled() or self._due(waiter) > now:
                continue
            if first is None:
                first = waiter
            if waiter.turn is self._last_turn:
                if same is None:
                    same = waiter
            elif other is None:
                other = waiter
        if self._boundary(now):
            return other or first
        return same or first

    async def _wait_until_ready(self, delay: float) -> None:
        wake = asyncio.get_running_loop().create_future()
        timer = asyncio.ensure_future(self._sleep(delay))
        self._wake, self._timer = wake, timer
        try:
            await asyncio.wait((wake, timer), return_when=asyncio.FIRST_COMPLETED)
            if timer.done():
                timer.result()
        finally:
            wake.cancel()
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)
            self._wake, self._timer = None, None

    async def _run(self) -> None:
        try:
            while not self._closed and self._holder is None and self._waiters:
                now = self._clock()
                chosen = self._choose(now)
                if chosen is None:
                    delay = max(0.0, min(self._due(waiter) for waiter in self._waiters) - now)
                    await self._wait_until_ready(delay)
                    continue
                self._waiters.remove(chosen)
                self._holder = chosen
                chosen.future.set_result(None)
                return
        except BaseException as exc:
            self._closed = True
            for waiter in self._waiters:
                if not waiter.future.done():
                    if isinstance(exc, asyncio.CancelledError):
                        waiter.future.cancel()
                    else:
                        waiter.future.set_exception(exc)
            self._waiters.clear()
        finally:
            self._pump = None

    def _start(self, waiter: _Waiter) -> DeliveryPermit:
        now = self._clock()
        new_group = waiter.turn is not self._last_turn or self._boundary(now)
        reply = new_group or self._force_reply or self._last_send is None
        if new_group:
            # Rotate the entire previous turn, not just one request, for fair bursts.
            previous = self._last_turn
            following = [item for item in self._waiters if item.turn is previous]
            self._waiters = deque(item for item in self._waiters if item.turn is not previous)
            self._waiters.extend(following)
            self._group_started = now
            self._group_count = 0
        self._last_turn = waiter.turn
        self._group_count += 1
        waiter.revision = self._revision
        return DeliveryPermit(reply)

    @asynccontextmanager
    async def slot(
        self, turn: object, *, not_before: float = 0.0, interval: float = 1.2
    ) -> AsyncIterator[DeliveryPermit]:
        if self._closed:
            raise RuntimeError("Delivery queue is closed")
        owner = asyncio.current_task()
        if owner is None:
            raise RuntimeError("Delivery requires an active task")
        waiter = _Waiter(turn, not_before, interval, owner, asyncio.get_running_loop().create_future())
        self._waiters.append(waiter)
        self._signal()
        entered = successful = False
        try:
            await waiter.future
            if self._closed:
                raise asyncio.CancelledError
            permit = self._start(waiter)
            entered = True
            yield permit
            successful = True
        finally:
            with suppress(ValueError):
                self._waiters.remove(waiter)
            if self._holder is waiter:
                self._holder = None
                if entered:
                    self._last_send = self._clock()
                    self._force_reply = not successful or waiter.revision != self._revision
                    if not successful:
                        self._last_turn = None
                        self._group_started = None
            self._signal()

    def interrupt(self) -> None:
        self._revision += 1
        self._force_reply = True
        self._last_turn = None
        self._group_started = None
        self._signal()

    def cancel_turn(self, turn: object) -> None:
        retained = deque()
        for waiter in self._waiters:
            if waiter.turn is turn:
                waiter.future.cancel()
            else:
                retained.append(waiter)
        self._waiters = retained
        self._signal()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for waiter in self._waiters:
            waiter.future.cancel()
        self._waiters.clear()
        if self._pump is not None:
            self._pump.cancel()
        if self._holder is not None and self._holder.owner is not asyncio.current_task():
            self._holder.owner.cancel()

    async def wait_closed(self) -> None:
        """Drain scheduling housekeeping after close; transport remains caller-owned."""
        if self._pump is not None:
            await asyncio.gather(self._pump, return_exceptions=True)
