"""Temporary in-memory wire for genuine Satori Account/Entari Session objects."""

from __future__ import annotations

from typing import Any
from datetime import datetime, timezone

from satori import EventType, ChannelType
from satori.const import Api
from satori.model import User, Event, Login, Channel, MessageObject
from arclet.entari import command
from worker_rendering import MediaValidationError, safe_effect
from arclet.entari.session import Session, EntariProtocol
from satori.client.account import Account, ApiInfo
from arclet.entari.event.base import MessageCreatedEvent


class CaptureProtocol(EntariProtocol):
    def __init__(self, account):
        super().__init__(account)
        self.messages: list[str] = []
        self.media_errors: list[str] = []
        self.expected = ""
        self.matched_expected = False
        self.match_tail = ""

    async def call_api(
        self, action: str | Api, params: dict | None = None, multipart: bool = False, method: str = "POST"
    ) -> Any:
        # Satori actions return objects or arrays despite the SDK's dict annotation.
        params = params or {}
        if action == Api.CHANNEL_GET:
            return {"id": params["channel_id"], "type": ChannelType.TEXT.value}
        if action == Api.USER_CHANNEL_CREATE:
            return {"id": "private-" + params["user_id"], "type": ChannelType.DIRECT.value}
        if action == Api.MESSAGE_CREATE:
            content = str(params["content"])
            _capture(self, content)
            return [{"id": f"sent-{len(self.messages)}", "content": content}]
        raise RuntimeError(f"Synthetic acceptance transport does not support API {action}")


def _capture(protocol: CaptureProtocol, content: str) -> None:
    # Entari wraps every bound protocol method as an async API; keep local evidence outside it.
    joined = protocol.match_tail + content
    protocol.matched_expected |= protocol.expected in joined
    protocol.match_tail = (joined + "\n")[-len(protocol.expected) :] if protocol.expected else ""
    try:
        if len(protocol.messages) >= 64:
            raise MediaValidationError("Command exceeds the visible effect count limit")
        effect = safe_effect(content)
        if sum(map(len, protocol.messages)) + len(effect) > 65536:
            raise MediaValidationError("Command exceeds the total evidence limit")
        protocol.messages.append(effect)
    except MediaValidationError as exc:
        if len(protocol.media_errors) < 24:
            protocol.media_errors.append(str(exc))
        raise


class CaptureTransport:
    def __init__(self):
        login = Login(sn=0, platform="workshop", adapter="workshop", user=User("workshop-bot"))
        self.account = Account(login, ApiInfo(), [], CaptureProtocol)
        self.sequence = 0

    async def execute(self, text: str, operator: bool = False, expected_contains: str = "") -> tuple[str, ...]:
        self.sequence += 1
        messages = self.account.protocol.messages
        messages.clear()
        protocol = self.account.protocol
        protocol.media_errors.clear()
        protocol.expected = expected_contains
        protocol.matched_expected = False
        protocol.match_tail = ""
        origin = Event(
            EventType.MESSAGE_CREATED,
            datetime.now(timezone.utc),
            self.account.self_info,
            channel=Channel("workshop-channel", ChannelType.TEXT),
            user=User("workshop-operator" if operator else "workshop-user"),
            message=MessageObject(f"incoming-{self.sequence}", text),
        )
        session: Session = Session(self.account, MessageCreatedEvent(self.account, origin))
        result = await command.execute(text, session)
        if result is not None:
            _capture(protocol, str(result))
        if protocol.media_errors:
            raise MediaValidationError("; ".join(protocol.media_errors))
        return tuple(messages)

    async def aclose(self):
        await self.account.protocol.session.close()
