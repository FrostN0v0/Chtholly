"""Native Entari delivery contracts for reply attribution and cancellation."""

from __future__ import annotations

from types import SimpleNamespace
import asyncio
from datetime import datetime
from contextvars import ContextVar

import pytest
from satori import At, File, User, Audio, Event, Image, Login, Quote, Channel, EventType, ChannelType
from satori.const import Api
from satori.model import MessageObject
from arclet.entari import Session, MessageChain, MessageCreatedEvent
from satori.client import Account, ApiInfo
import pytest_asyncio
from arclet.letoderea import BLOCK, Scope, Contexts, es
from arclet.entari.config import EntariConfig
from arclet.entari.session import EntariProtocol
from arclet.entari.event.api import SendRequest
from satori.adapters.onebot11.message import OneBot11MessageEncoder

from plugins.llm_chat.channel_turns import latest_participant_turn
from plugins.llm_chat.core.delivery import DeliveryState
from plugins.llm_chat.group_delivery import (
    group_delivery_scope,
    finish_group_delivery,
    install_group_delivery,
)
from plugins.llm_chat.tools._delivery import send_with_delivery, build_forward_chain

_OWNER: ContextVar[str] = ContextVar("test_delivery_owner", default="outside")


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await asyncio.sleep(0)


@pytest_asyncio.fixture
async def transport(monkeypatch, tmp_path):
    if not hasattr(EntariConfig, "instance"):
        config = tmp_path / "entari.yml"
        config.write_text("basic: {}\nplugins: {}\n", encoding="utf-8")
        EntariConfig.instance = EntariConfig.load(config)
    clock = Clock()
    login = Login(sn=0, platform="onebot", adapter="onebot", user=User("10", name="Bot"))
    account = Account(login, ApiInfo(), [], EntariProtocol)
    network = SimpleNamespace(wire=[], hold="", entered=asyncio.Event(), gate=asyncio.Event(), outcome="success")
    prepared = {}
    observer = Scope.of()

    async def capture(event: SendRequest) -> None:
        if event.account is account:
            prepared[str(event.message)] = event.message

    observer.register(capture, event=SendRequest, priority=-1000)

    async def call_api(action, params):
        if action == Api.CHANNEL_GET:
            return {"id": params["channel_id"], "type": ChannelType.TEXT.value}
        assert action == Api.MESSAGE_CREATE
        message = prepared[params["content"]]
        network.wire.append((message, _OWNER.get()))
        if network.hold and message.extract_plain_text() == network.hold:
            network.entered.set()
            await network.gate.wait()
        if network.outcome == "empty":
            return []
        return [{"id": f"sent-{len(network.wire)}", "content": params["content"]}]

    monkeypatch.setattr(account.protocol, "call_api", call_api)

    def session(message_id="incoming", *, user="20", channel="100", direct=False):
        origin = Event(
            EventType.MESSAGE_CREATED,
            datetime.now(),
            account.self_info,
            channel=Channel(channel, ChannelType.DIRECT if direct else ChannelType.TEXT),
            user=User(user, name="Same name"),
            message=MessageObject(message_id, "Question"),
        )
        return Session(account, MessageCreatedEvent(account, origin))

    def state():
        return DeliveryState(clock=clock, sleep=clock.sleep)

    dispose = install_group_delivery(clock=clock, sleep=clock.sleep, transport_timeout=0.2)
    try:
        yield SimpleNamespace(account=account, login=login, session=session, state=state, clock=clock, network=network)
    finally:
        await dispose()
        disposals = observer.dispose()
        if disposals:
            await asyncio.gather(*disposals)
        await account.protocol.session.close()


def quotes(chain: MessageChain) -> list[str]:
    return [element.id for element in chain if isinstance(element, Quote)]


async def test_native_reply_target_is_immutable_and_continuation_is_not_requoted(transport) -> None:
    session, state = transport.session("original"), transport.state()
    token = _OWNER.set("original-owner")
    try:
        with group_delivery_scope(session):
            session.event.message.id = "changed-after-claim"
            await send_with_delivery(session, "first", state, texts=["first"])
            clone = Session(session.account, session.event)
            await send_with_delivery(clone, "second", state, texts=["second"])
    finally:
        _OWNER.reset(token)
    wire = transport.network.wire
    assert [quotes(chain) for chain, _ in wire] == [["original"], []]
    assert [owner for _, owner in wire] == ["original-owner", "original-owner"]
    assert not any(chain.get(At) for chain, _ in wire)
    assert state.delivery_attempts == state.confirmed_deliveries == 2
    assert state.delivered_texts == ["first", "second"]


async def test_same_channel_incoming_and_unrelated_outgoing_break_continuity(transport) -> None:
    session, state = transport.session("question-a"), transport.state()
    with group_delivery_scope(session):
        await send_with_delivery(session, "a1", state)
        await transport.session("other-channel", channel="200").send("elsewhere")
        await send_with_delivery(session, "a2", state)
        await es.publish(transport.session("unaddressed", user="30").event)
        await send_with_delivery(session, "a3", state)
        await transport.session("other-command", user="40").send("notice")
        await send_with_delivery(session, "a4", state)
    replies = {chain.extract_plain_text(): quotes(chain) for chain, _ in transport.network.wire}
    assert replies == {
        "a1": ["question-a"],
        "elsewhere": [],
        "a2": [],
        "a3": ["question-a"],
        "notice": [],
        "a4": ["question-a"],
    }


async def test_distinct_same_named_people_and_private_messages_keep_separate_targets(transport) -> None:
    first = transport.session("first-request", user="20")
    second = transport.session("second-request", user="30")
    for session, text in ((first, "first"), (second, "second")):
        with group_delivery_scope(session):
            await send_with_delivery(session, text, transport.state())
    direct = transport.session("private-request", channel="private:20", direct=True)
    with group_delivery_scope(direct):
        await send_with_delivery(direct, "private", transport.state())
    assert [quotes(chain) for chain, _ in transport.network.wire] == [["first-request"], ["second-request"], []]


async def test_deferred_child_of_closed_scope_cannot_fall_back_to_unscoped_send(transport) -> None:
    session, state = transport.session(), transport.state()
    gate = asyncio.Event()

    async def deferred() -> None:
        await gate.wait()
        await send_with_delivery(session, "stale", state)

    with group_delivery_scope(session):
        child = asyncio.create_task(deferred())
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await child
    assert transport.network.wire == []
    assert state.delivery_attempts == state.confirmed_deliveries == 0


async def test_latest_wins_removes_queued_old_reply_without_cancelling_other_user(transport) -> None:
    transport.network.hold = "holding"
    states = {}
    queued = asyncio.Event()

    async def handler(session, _ctx):
        state = states.setdefault(session.event.message.id, transport.state())
        with group_delivery_scope(session):
            text = "holding" if session.event.message.id == "other-user" else session.event.message.id
            if text == "old-request":
                queued.set()
            await send_with_delivery(session, text, state, texts=[text])
        return BLOCK

    wrapped = latest_participant_turn(handler)
    other = asyncio.create_task(wrapped(transport.session("other-user", user="30"), Contexts()))
    await transport.network.entered.wait()
    old = asyncio.create_task(wrapped(transport.session("old-request"), Contexts()))
    await queued.wait()
    newer = asyncio.create_task(wrapped(transport.session("new-request"), Contexts()))
    try:
        await asyncio.wait_for(old, timeout=1)
        assert not other.done()
        assert states["old-request"].delivery_attempts == 0
        transport.network.gate.set()
        await asyncio.wait_for(asyncio.gather(other, newer), timeout=1)
    finally:
        for task in (other, old, newer):
            if not task.done():
                task.cancel()
        await asyncio.gather(other, old, newer, return_exceptions=True)
    wire = transport.network.wire
    assert [chain.extract_plain_text() for chain, _ in wire] == ["holding", "new-request"]
    assert [quotes(chain) for chain, _ in wire] == [["other-user"], ["new-request"]]


async def test_transport_timeout_is_unknown_once_and_releases_the_channel(transport) -> None:
    transport.network.hold = "timeout"
    first, failed = transport.session("first"), transport.state()
    with group_delivery_scope(first), pytest.raises(asyncio.TimeoutError):
        await send_with_delivery(first, "timeout", failed, texts=["timeout"])
    assert failed.delivery_attempts == 1
    assert failed.confirmed_deliveries == 0
    assert failed.delivered_texts == []
    second, succeeded = transport.session("second", user="30"), transport.state()
    with group_delivery_scope(second):
        await send_with_delivery(second, "next", succeeded, texts=["next"])
    assert [chain.extract_plain_text() for chain, _ in transport.network.wire] == ["timeout", "next"]
    assert quotes(transport.network.wire[-1][0]) == ["second"]
    assert succeeded.delivery_attempts == succeeded.confirmed_deliveries == 1


async def test_empty_receipts_cannot_be_recorded_as_success(transport) -> None:
    transport.network.outcome = "empty"
    session, state = transport.session(), transport.state()
    from plugins.llm_chat.core.delivery import DeliveryError

    with group_delivery_scope(session), pytest.raises(DeliveryError):
        await send_with_delivery(session, "unconfirmed", state, texts=["unconfirmed"])
    assert state.delivery_attempts == 1
    assert state.confirmed_deliveries == 0
    assert state.delivered_texts == []


@pytest.mark.parametrize("kind", ["audio", "forward", "file"])
async def test_special_media_finishes_with_one_real_quote_not_an_empty_protocol_message(transport, kind) -> None:
    if kind == "audio":
        payload = MessageChain([Audio.of(raw=b"audio", mime="audio/wav")])
        expected_action = "send_group_msg"
    elif kind == "file":
        payload = MessageChain([File.of(raw=b"archive", mime="application/zip", title="source.zip")])
        expected_action = "upload_group_file"
    else:
        payload = build_forward_chain(["first node", "second node"])
        expected_action = "send_group_forward_msg"
    session, state = transport.session("media-request"), transport.state()
    with group_delivery_scope(session):
        await send_with_delivery(session, payload, state, media=kind != "forward")
        assert len(transport.network.wire) == 1
        assert quotes(transport.network.wire[0][0]) == []
        await finish_group_delivery(session, state)
    assert len(transport.network.wire) == 2
    assert quotes(transport.network.wire[1][0]) == ["media-request"]
    assert state.delivery_attempts == state.confirmed_deliveries == 2
    assert state.text_messages == 1
    assert state.text_chars == len(state.delivered_texts[0])
    calls = []

    class Network:
        async def call_api(self, action, params):
            calls.append((action, params))
            return {"message_id": str(len(calls))}

    for message, _ in transport.network.wire:
        encoder = OneBot11MessageEncoder(transport.login, Network(), "100")
        await encoder.send(str(message))
    assert [action for action, _ in calls] == [expected_action, "send_group_msg"]
    assert [part["type"] for part in calls[-1][1]["message"]] == ["reply", "text"]


async def test_quoted_image_attributes_prior_audio_without_preempting_media_with_text(transport) -> None:
    session, state = transport.session("media"), transport.state()
    with group_delivery_scope(session):
        await send_with_delivery(session, MessageChain([Audio.of(raw=b"audio", mime="audio/wav")]), state, media=True)
        await send_with_delivery(session, MessageChain([Image.of(raw=b"image", mime="image/png")]), state, media=True)
        await finish_group_delivery(session, state)
    wire = transport.network.wire
    assert len(wire) == 2
    assert [quotes(chain) for chain, _ in wire] == [[], ["media"]]
    assert state.confirmed_media_deliveries == 2
    assert state.text_messages == 0
    assert state.delivered_texts == []


async def test_old_runtime_disposal_does_not_close_replacement_delivery(transport) -> None:
    old = install_group_delivery(clock=transport.clock, sleep=transport.clock.sleep)
    replacement = install_group_delivery(clock=transport.clock, sleep=transport.clock.sleep)
    session, state = transport.session("replacement"), transport.state()
    try:
        with group_delivery_scope(session):
            await send_with_delivery(session, "first", state)
            await old()
            await send_with_delivery(session, "second", state)
        assert [quotes(chain) for chain, _ in transport.network.wire] == [["replacement"], []]
    finally:
        await replacement()
        await old()
