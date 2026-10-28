"""Build a host-origin transport context without inventing a human message."""

from __future__ import annotations

from typing import Any
from datetime import UTC, datetime

from satori import User, Event, Guild, Channel, ChannelType
from arclet.entari import Session
from arclet.entari.event.base import InternalEvent

from ..models import ChatScope


def host_dialogue_session(account: Any, scope: ChatScope, channel: Channel) -> Session:
    if (
        str(account.platform) != scope.platform
        or str(account.self_id) != scope.account_id
        or channel.id != scope.channel_id
        or channel.type not in {ChannelType.TEXT, ChannelType.DIRECT}
    ):
        raise ValueError("Host dialogue transport does not belong to its native scope")
    origin = Event(
        type="internal",
        timestamp=datetime.now(UTC),
        login=account.self_info,
        channel=channel,
        guild=Guild(id=scope.guild_id) if scope.guild_id and channel.type != ChannelType.DIRECT else None,
        user=User(id=str(account.self_id), name=""),
        message=None,
    )
    return Session(account, InternalEvent(account, origin))
