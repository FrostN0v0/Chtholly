from kirami import on_prefix, on_endswith
from kirami.typing import Matcher
from kirami.message import MessageSegment, Message
from kirami.event import MessageEvent
from kirami.service.manager import _from_file as from_file

from operator import itemgetter
from itertools import groupby
from nonebot.plugin import get_loaded_plugins, Plugin
from kirami.utils.utils import tpl2img, md2img
from pathlib import Path

menu = on_prefix("功能", "菜单", "help")


@menu.handle()
async def menu(matcher: Matcher):
    pcls = []
    for p in list(get_loaded_plugins()):
        plug = from_file(p)
        pcls.append(plug)
    pcls = sorted(pcls, key=itemgetter('tags'))
    plugin_dict = {}
    for date, items in groupby(pcls, key=itemgetter('tags')):
        plugin_dict[list(date)[0]] = list(items)
    img = await tpl2img(tpl=Path(__file__).parent / "templates" / "menu.html", width=1903, data={'plugin_dict': plugin_dict}, base_path=str(Path(__file__).parent / "templates"), wait=2, height=975)
    await matcher.finish(MessageSegment.image(img))

md_help = on_endswith("帮助", priority=15)


@md_help.handle()
async def md2help(matcher: Matcher, event: MessageEvent):
    arg = event.get_plaintext().split('帮助')[0]
    for p in list(get_loaded_plugins()):
        plug = from_file(p)
        if plug['name'] == arg:
            md_path = Path(p.module.__path__[0]) / "README.md"
            img = await md2img(md=md_path, width=800)
            await matcher.finish(message=MessageSegment.image(img))
