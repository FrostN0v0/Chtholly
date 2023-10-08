from kirami import on_prefix
from kirami.typing import Matcher
from kirami.message import MessageSegment
from kirami.service.manager import _from_file as from_file

from operator import itemgetter
from itertools import groupby
from nonebot.plugin import get_loaded_plugins
from kirami.utils.utils import tpl2img
from pathlib import Path


@on_prefix("帮助", "菜单", "help")
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
