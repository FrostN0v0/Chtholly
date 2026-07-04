"""Poke response plugin.

Listens for OneBot V11 poke notices forwarded by the satori onebot11 adapter
as INTERNAL events (raw type ``notice.notify.poke``) and replies with text,
image, audio, or a poke back depending on configured probabilities.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING, Any

from arclet.entari import Account, Plugin, Session, metadata, register_internal_event
from arclet.entari.event.base import Attr, NoticeEvent
from satori import EventType
from satori.element import Audio, Custom, Image
from satori.model import Channel, ChannelType, Event, Guild, User

from utils.path import AUDIO_DIR, IMAGE_DIR

if TYPE_CHECKING:
    from pathlib import Path

metadata(
    name="poke",
    author=[{"name": "FrostN0v0"}],
    version="0.1.0",
    description="Reply to OneBot V11 poke notices with text, image, audio, or poke back.",
)

plug = Plugin.current()

DINGGONG_DIR: Path = AUDIO_DIR / "dinggong"
FOX_DIR: Path = IMAGE_DIR / "fox_img"

POKE_REPLIES: list[str] = [
    "lsp你再戳？",
    "连个可爱美少女都要戳的肥宅真恶心啊。",
    "你再戳！",
    "？再戳试试？",
    "别戳了别戳了再戳就坏了555",
    "我爪巴爪巴，球球别再戳了",
    "你戳你🐎呢？！",
    "那...那里...那里不能戳...绝对...",
    "(。´・ω・)ん?",
    "有事恁叫我，别天天一个劲戳戳戳！",
    "欸很烦欸！你戳🔨呢",
    "?",
    "再戳一下试试？",
    "???",
    "正在关闭对您的所有服务...关闭成功",
    "啊呜，太舒服刚刚竟然睡着了。什么事？",
    "正在定位您的真实地址...定位成功。轰炸机已起飞",
]


class PokeEvent(NoticeEvent):
    """Custom event wrapping an OneBot V11 ``notice.notify.poke`` notice.

    The satori onebot11 adapter forwards unregistered notices as
    ``EventType.INTERNAL`` with the raw event data attached. This class
    materializes that into a proper notice event with channel/user context so
    handlers can reply through a normal ``Session``.
    """

    type = EventType.INTERNAL

    user_id: str = Attr()
    target_id: str = Attr()
    self_id: str = Attr()
    group_id: str | None = Attr()

    def __init__(self, account: Account, origin: Event):
        super().__init__(account, origin)
        data: dict[str, Any] = origin._data or {}
        self.user_id = str(data.get("user_id", ""))
        self.target_id = str(data.get("target_id", ""))
        self.self_id = str(data.get("self_id", ""))
        group_id = data.get("group_id")
        self.group_id = str(group_id) if group_id is not None else None

        # Rebuild channel/user context so Session.send has a target.
        if self.group_id:
            self.channel = Channel(str(self.group_id), ChannelType.TEXT)
            self.guild = Guild(str(self.group_id))
        else:
            self.channel = Channel(f"private:{self.self_id}", ChannelType.DIRECT)
            self.guild = None
        self.user = User(self.user_id)


@register_internal_event
def _parse_poke(event_type: str, raw_type: str, data: dict[str, Any]) -> type[NoticeEvent] | None:
    if event_type == EventType.INTERNAL.value and raw_type == "notice.notify.poke":
        return PokeEvent
    return None


_img_list: list[Path] = [p for p in FOX_DIR.iterdir() if p.is_file()]
_audio_list: list[Path] = [p for p in DINGGONG_DIR.iterdir() if p.is_file()]


@plug.dispatch(PokeEvent)
async def on_poke(session: Session[PokeEvent]):
    event = session.event
    if event.target_id != event.self_id:
        return

    if random.random() < 0.3:
        prefix = "气死我了！" if random.random() < 0.15 else ""
        await session.send(prefix + random.choice(POKE_REPLIES), at_sender=True)
        return

    roll = random.random()
    if roll <= 0.3:
        await session.send(Image.of(path=random.choice(_img_list)))
    elif roll < 0.6:
        await session.send(Audio.of(path=random.choice(_audio_list)))
    else:
        await session.send(Custom("onebot:poke", {"qq": event.user_id}))
