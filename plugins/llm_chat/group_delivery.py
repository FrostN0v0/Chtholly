"""Ready-only group delivery and immutable native reply attribution."""

from __future__ import annotations

import time
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import field, dataclass
from collections.abc import Callable, Iterator, Sequence, Awaitable

from satori import ChannelType
from arclet.entari import Session, MessageChain, MessageCreatedEvent
from arclet.letoderea import Scope
from arclet.entari.event.api import SendResponse

from utils.group_delivery_core import DeliveryQueue

from .core.delivery import (
    DEFAULT_DELIVERY_LIMITS,
    DeliveryError,
    DeliveryState,
    mark_delivery_attempt,
    mark_delivery_success,
    normalize_delivery_delay,
    reserve_attribution_text,
    delivery_interval_seconds,
)
from .reply_payload import quote_request, supports_reply

_ChannelKey = tuple[str, str, str]
_IDLE_CHANNEL_SECONDS = 8.0


@dataclass
class _Channel:
    queue: DeliveryQueue
    references: int = 0
    sends: set[asyncio.Task[object]] = field(default_factory=set)
    expiry: asyncio.TimerHandle | None = None


@dataclass
class _Runtime:
    clock: Callable[[], float]
    sleep: Callable[[float], Awaitable[None]]
    transport_timeout: float
    channels: dict[_ChannelKey, _Channel] = field(default_factory=dict)
    closed: bool = False

    def release(self, key: _ChannelKey, channel: _Channel) -> None:
        if self.closed or channel.references or channel.sends or channel.expiry is not None:
            return

        def expire() -> None:
            channel.expiry = None
            if not channel.references and not channel.sends and self.channels.get(key) is channel:
                channel.queue.close()
                del self.channels[key]

        channel.expiry = asyncio.get_running_loop().call_later(_IDLE_CHANNEL_SECONDS, expire)


@dataclass
class _Turn:
    runtime: _Runtime
    key: _ChannelKey
    channel: _Channel
    source: MessageCreatedEvent
    message_id: str
    token: object = field(default_factory=object)
    closed: bool = False
    pending_attribution: bool = False
    last_send: float | None = None
    sends: set[asyncio.Task[object]] = field(default_factory=set)


_CURRENT: ContextVar[_Turn | None] = ContextVar("llm_chat_group_delivery", default=None)
_SENDING: ContextVar[_Turn | None] = ContextVar("llm_chat_group_transport", default=None)
_RUNTIME: _Runtime | None = None


def _key(session: Session) -> _ChannelKey:
    return session.account.platform, session.account.self_id, session.channel.id


@contextmanager
def group_delivery_scope(session: Session) -> Iterator[None]:
    """Bind the full claimed turn, without reserving any channel sending time."""
    runtime = _RUNTIME
    source = session.event
    if (
        runtime is None
        or runtime.closed
        or not isinstance(source, MessageCreatedEvent)
        or source.channel is None
        or source.channel.type == ChannelType.DIRECT
        or source.message is None
        or not source.message.id
    ):
        yield
        return
    key = _key(session)
    channel = runtime.channels.get(key)
    if channel is None:
        channel = _Channel(DeliveryQueue(clock=runtime.clock, sleep=runtime.sleep))
        runtime.channels[key] = channel
    if channel.expiry is not None:
        channel.expiry.cancel()
        channel.expiry = None
    channel.references += 1
    turn = _Turn(runtime, key, channel, source, str(source.message.id))
    token = _CURRENT.set(turn)
    try:
        yield
    finally:
        turn.closed = True
        channel.queue.cancel_turn(turn.token)
        for task in tuple(turn.sends):
            if task is not asyncio.current_task():
                task.cancel()
        channel.references -= 1
        runtime.release(key, channel)
        _CURRENT.reset(token)


def _turn_for(session: Session) -> _Turn | None:
    turn = _CURRENT.get()
    if turn is None or session.event is not turn.source or _key(session) != turn.key:
        return None
    if turn.closed or turn.runtime.closed:
        raise asyncio.CancelledError
    return turn


async def send_group_delivery(
    session: Session,
    payload: str | MessageChain,
    state: DeliveryState | None,
    *,
    delay_seconds: float | None = None,
    texts: Sequence[str] = (),
    media: bool = False,
) -> bool:
    """Schedule prepared content only; preserve the caller's audit and authorization context."""
    turn = _turn_for(session)
    if turn is None:
        return False
    content = MessageChain(payload) if isinstance(payload, str) else payload
    limits = state.limits if state is not None else DEFAULT_DELIVERY_LIMITS
    interval = delivery_interval_seconds(limits, normalize_delivery_delay(delay_seconds))
    now = turn.runtime.clock()
    not_before = now if turn.last_send is None else max(now, turn.last_send + interval)
    if state is not None and state.last_delivery_at is not None:
        remaining = interval - max(0.0, state.clock() - state.last_delivery_at)
        not_before = max(not_before, now + remaining)
    task = asyncio.current_task()
    if task is None:
        raise RuntimeError("Group delivery requires an active task")
    turn.sends.add(task)
    turn.channel.sends.add(task)
    try:
        async with turn.channel.queue.slot(turn.token, not_before=not_before, interval=interval) as permit:
            if turn.closed or turn.runtime.closed:
                raise asyncio.CancelledError
            quotable = supports_reply(content, turn.key[0])
            quoted = quotable and (permit.reply or turn.pending_attribution)
            outgoing = quote_request(content, turn.message_id) if quoted else content
            started = False

            async def transmit():
                nonlocal started
                token = _SENDING.set(turn)
                try:
                    started = True
                    return await session.send(outgoing)
                finally:
                    _SENDING.reset(token)

            try:
                receipts = await asyncio.wait_for(transmit(), timeout=turn.runtime.transport_timeout)
                if not receipts:
                    raise DeliveryError("Transport returned no confirmed delivery receipt")
            except BaseException:
                if started and state is not None:
                    mark_delivery_attempt(state)
                raise
            if state is not None:
                mark_delivery_success(state, texts, media=media)
            turn.last_send = turn.runtime.clock()
            if quoted:
                turn.pending_attribution = False
            elif not quotable and permit.reply:
                turn.pending_attribution = True
            return True
    finally:
        turn.sends.discard(task)
        turn.channel.sends.discard(task)
        turn.runtime.release(turn.key, turn.channel)


async def finish_group_delivery(session: Session, state: DeliveryState) -> None:
    """Attribute standalone non-quotable media only after all successful turn output."""
    turn = _turn_for(session)
    if turn is None or not turn.pending_attribution:
        return
    if state.delivery_attempts != state.confirmed_deliveries:
        return
    text = reserve_attribution_text(state, "Reply to this request.")
    await send_group_delivery(session, text, state, texts=[text])


def install_group_delivery(
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    transport_timeout: float = 30.0,
) -> Callable[[], Awaitable[None]]:
    """Own interruption observers, scopes and cancellable transports for one plugin load."""
    global _RUNTIME
    previous = _RUNTIME
    runtime = _Runtime(clock, sleep, transport_timeout)
    _RUNTIME = runtime
    scope = Scope.of()

    async def incoming(event: MessageCreatedEvent) -> None:
        if runtime.closed or event.channel is None or event.user is None:
            return
        if event.user.id == event.account.self_id:
            return
        key = event.account.platform, event.account.self_id, event.channel.id
        channel = runtime.channels.get(key)
        if channel is not None:
            channel.queue.interrupt()

    async def outgoing(event: SendResponse) -> None:
        if runtime.closed or not event.result:
            return
        key = event.account.platform, event.account.self_id, event.channel
        sender = _SENDING.get()
        if sender is not None and sender.key == key and event.session is not None:
            if sender.source is event.session.event:
                return
        channel = runtime.channels.get(key)
        if channel is not None:
            channel.queue.interrupt()

    scope.register(incoming, event=MessageCreatedEvent, priority=-1000)
    scope.register(outgoing, event=SendResponse, priority=-1000)

    async def dispose() -> None:
        global _RUNTIME
        if runtime.closed:
            return
        runtime.closed = True
        if _RUNTIME is runtime:
            _RUNTIME = previous if previous is not None and not previous.closed else None
        tasks = set()
        for channel in runtime.channels.values():
            if channel.expiry is not None:
                channel.expiry.cancel()
            channel.queue.close()
            tasks.update(task for task in channel.sends if task is not asyncio.current_task())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(channel.queue.wait_closed() for channel in runtime.channels.values()))
        runtime.channels.clear()
        disposals = scope.dispose()
        if disposals:
            await asyncio.gather(*disposals, return_exceptions=True)

    return dispose
