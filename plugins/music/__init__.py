import traceback

from kirami import on_command
from kirami.log import logger
from kirami.matcher import Matcher
from kirami.depends import CommandArg

from .data_source import *

check_qq = on_command("点歌", "qq点歌", "QQ点歌", priority=20)
check_163 = on_command("163点歌", "网易点歌", "网易云点歌")
check_kuwo = on_command("kuwo点歌", "酷我点歌")
check_kugou = on_command("kugou点歌", "酷狗点歌")
check_migu = on_command("migu点歌", "咪咕点歌")
check_bili = on_command("bili点歌", "bilibili点歌", "b站点歌", "B站点歌")

func_list = ["QQ音乐", "网易云", "酷我", "酷狗", "咪咕", "B站"]


async def search(func: str, keyword: str):
    if func in func_list:
        print(func)
        match func:
            case "QQ音乐":
                return await search_qq(keyword)
            case "网易云":
                return await search_163(keyword)
            case "酷我":
                return await search_kuwo(keyword)
            case "酷狗":
                return await search_kugou(keyword)
            case "咪咕":
                return await search_migu(keyword)
            case "B站":
                return await search_bili(keyword)


@check_qq.handle()
async def _(matcher: Matcher, msg: CommandArg):
    keyword = msg.extract_plain_text().strip()
    if not keyword:
        matcher.block = False
        await matcher.finish()
    try:
        res = await search("QQ音乐", keyword)
        if not res:
            res = f"QQ音乐中找不到相关的歌曲"
    except Exception:
        logger.warning(traceback.format_exc())
        res = "出错了，请稍后再试"
    await matcher.finish(res)


@check_163.handle()
async def _(matcher: Matcher, msg: CommandArg):
    keyword = msg.extract_plain_text().strip()
    if not keyword:
        matcher.block = False
        await matcher.finish()
    try:
        res = await search("网易云", keyword)
        if not res:
            res = f"网易云音乐中找不到相关的歌曲"
    except Exception:
        logger.warning(traceback.format_exc())
        res = "出错了，请稍后再试"
    await matcher.finish(res)


@check_bili.handle()
async def _(matcher: Matcher, msg: CommandArg):
    keyword = msg.extract_plain_text().strip()
    if not keyword:
        matcher.block = False
        await matcher.finish()
    try:
        res = await search("B站", keyword)
        if not res:
            res = f"哔哩哔哩音乐中找不到相关的歌曲"
    except Exception:
        logger.warning(traceback.format_exc())
        res = "出错了，请稍后再试"
    await matcher.finish(res)


@check_kuwo.handle()
async def _(matcher: Matcher, msg: CommandArg):
    keyword = msg.extract_plain_text().strip()
    if not keyword:
        matcher.block = False
        await matcher.finish()
    try:
        res = await search("酷我", keyword)
        if not res:
            res = f"酷我音乐中找不到相关的歌曲"
    except Exception:
        logger.warning(traceback.format_exc())
        res = "出错了，请稍后再试"
    await matcher.finish(res)


@check_migu.handle()
async def _(matcher: Matcher, msg: CommandArg):
    keyword = msg.extract_plain_text().strip()
    if not keyword:
        matcher.block = False
        await matcher.finish()
    try:
        res = await search("咪咕", keyword)
        if not res:
            res = f"咪咕音乐中找不到相关的歌曲"
    except Exception:
        logger.warning(traceback.format_exc())
        res = "出错了，请稍后再试"
    await matcher.finish(res)


@check_kugou.handle()
async def _(matcher: Matcher, msg: CommandArg):
    keyword = msg.extract_plain_text().strip()
    if not keyword:
        matcher.block = False
        await matcher.finish()
    try:
        res = await search("酷狗", keyword)
        if not res:
            res = f"酷狗音乐中找不到相关的歌曲"
    except Exception:
        logger.warning(traceback.format_exc())
        res = "出错了，请稍后再试"
    await matcher.finish(res)
