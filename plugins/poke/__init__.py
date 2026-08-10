"""Poke response plugin.

Listens for OneBot V11 poke notices forwarded by the satori onebot11 adapter
as INTERNAL events (raw type ``notice.notify.poke``) and replies with text,
image, audio, or a poke back depending on configured probabilities.
"""

from __future__ import annotations

import random
from pathlib import Path
from collections.abc import Mapping

from satori import EventType
from satori.model import User, Event, Guild, Channel, ChannelType
from arclet.entari import (
    Plugin,
    Account,
    Session,
    MessageChain,
    attr,
    metadata,
    collect_disposes,
    register_internal_event,
)
from satori.element import Audio, Image
from arclet.entari.event.base import NoticeEvent

from utils.path import AUDIO_DIR, IMAGE_DIR

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
    ``EventType.INTERNAL`` with the raw event data attached to ``_data``.
    This class materializes that into a proper notice event with channel/user
    context so handlers can reply through a normal ``Session``.

    Field attributes are read from ``origin._data`` via ``attr(internal=True)``
    to avoid ``Attr.__set__`` raising (the bug in the previous etr version that
    declared them with ``Attr()`` and then tried to assign in ``__init__``).
    Channel/user/guild are not redeclared: their inherited ``Attr`` descriptors
    read from ``origin`` via ``getattr``, so we mutate ``origin`` directly.
    """

    type = EventType.INTERNAL

    user_id: str = attr(str, internal=True)
    target_id: str = attr(str, internal=True)
    self_id: str = attr(str, internal=True)
    group_id: str | None = attr(str, internal=True)

    def __init__(self, account: Account, origin: Event):
        super().__init__(account, origin)
        group_id = self.group_id
        if group_id:
            origin.channel = Channel(group_id, ChannelType.TEXT)
            origin.guild = Guild(group_id)
        else:
            origin.channel = Channel(f"private:{self.self_id}", ChannelType.DIRECT)
            origin.guild = None
        origin.user = User(self.user_id)


@register_internal_event
def _parse_poke(event_type: str, raw_type: str, data: Mapping[str, object]) -> type[NoticeEvent] | None:
    _ = data
    if event_type == EventType.INTERNAL.value and raw_type == "notice.notify.poke":
        return PokeEvent
    return None


def _remove_parser():
    from arclet.entari.event.base import INTERNAL_ADDITIONAL_HANDLERS

    INTERNAL_ADDITIONAL_HANDLERS[:] = [h for h in INTERNAL_ADDITIONAL_HANDLERS if h is not _parse_poke]


collect_disposes(_remove_parser)


_img_list: list[Path] | None = None
_audio_list: list[Path] | None = None


def _images() -> list[Path]:
    global _img_list
    if _img_list is None:
        _img_list = [p for p in FOX_DIR.iterdir() if p.is_file()] if FOX_DIR.exists() else []
    return _img_list


def _audios() -> list[Path]:
    global _audio_list
    if _audio_list is None:
        _audio_list = [p for p in DINGGONG_DIR.iterdir() if p.is_file()] if DINGGONG_DIR.exists() else []
    return _audio_list


def _int_id(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None


@plug.dispatch(PokeEvent)
async def on_poke(session: Session, event: PokeEvent):
    if event.target_id != event.self_id:
        return

    if random.random() < 0.3:
        prefix = "气死我了！" if random.random() < 0.15 else ""
        await session.send(prefix + random.choice(POKE_REPLIES), at_sender=True)
        return

    roll = random.random()
    if roll <= 0.3:
        imgs = _images()
        if imgs:
            await session.send(MessageChain([Image.of(path=random.choice(imgs))]))
    elif roll < 0.6:
        auds = _audios()
        if auds:
            await session.send(MessageChain([Audio.of(path=random.choice(auds))]))
    group_id = event.group_id
    user_id = _int_id(event.user_id)
    if user_id is None:
        return
    if group_id:
        parsed_group_id = _int_id(group_id)
        if parsed_group_id is not None:
            await session.account.internal("group_poke", group_id=parsed_group_id, user_id=user_id)
    else:
        await session.account.internal("friend_poke", user_id=user_id)
