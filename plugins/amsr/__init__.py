import aiohttp
from math import ceil
from kirami.utils.utils import md2img
from kirami import on_command
from kirami.typing import Matcher, State, Bot
from kirami.depends import Command, CommandArg, Arg, ArgPlainText
from kirami.message import Message, MessageSegment
from kirami.event import MessageEvent





headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0'
}


play = on_command("听音声", priority=5)
# stop = on_command("取消选音声", block=True, priority=5)
search = on_command("搜音声", priority=5)


@search.handle()
async def _search(bot: Bot, ev: MessageEvent, state: State, arg: CommandArg):
    y = 1
    keyword = ""
    title: list[str] = []
    rid: list[str] = []
    imgs: list[str] = []
    ars: list[str] = []
    arg = arg.extract_plain_text().strip().split()
    print(arg)
    if not arg:
        await search.send("请输入搜索关键词(用”/”分割不同tag)和搜索页数(可选)！比如“搜音声 伪娘/催眠 1”", at_sender=True)
        return
    if len(arg) == 1:
        keyword = arg[0].replace("/", "%20")
        y = 1
    elif len(arg) == 2:
        keyword = arg[0].replace("/", "%20")
        y = arg[1]
    elif len(arg) > 2:
        await search.send("请正确输入搜索关键词(用”/”分割不同tag)和搜索页数(可选)！比如“搜音声 伪娘/催眠 1”",
                          at_sender=True)
        return
    await search.send(f"正在搜索音声{keyword}，第{y}页！", at_sender=True)
    session = aiohttp.ClientSession()
    res = await session.get(
        f"https://api.asmr-200.com/api/search/{keyword}?order=dl_count&sort=desc&page={y}&subtitle=0&includeTranslationWorks=true",
        headers=headers, timeout=10)
    r = await res.json()
    if len(r["works"]) == 0:
        if r["pagination"]["totalCount"] == 0:
            await search.send("搜索结果为空", at_sender=True)
            return
        elif r["pagination"]["currentPage"] > 1:
            Count = int(r["pagination"]["totalCount"])
            a = ceil(Count / 20)
            await search.send(f"此搜索结果最多{a}页", at_sender=True)
            return
    for result2 in r["works"]:
        title.append(result2["title"])
        ars.append(result2["name"])
        imgs.append(result2["mainCoverUrl"])
        ids = str(result2["id"])
        if len(ids) == 7 or len(ids) == 5:
            ids = "RJ0" + ids
        else:
            ids = "RJ" + ids
        rid.append(ids)
    msg2 = f'### <div align="center">搜索结果</div>\n' \
           f'| 封面 | 序号 | RJ号 |\n' \
           '| --- | --- | --- |\n'
    msg = ""
    for i in range(len(title)):
        msg += str(i + 1) + ". 【" + rid[i] + "】 " + title[i] + "\n"
        msg2 += f'|<img width="250" src="{imgs[i]}"/> | {str(i + 1)}. |【{rid[i]}】|\n'
    msg += "请发送听音声+RJ号+节目编号（可选）来获取要听的资源"
    output = await md2img(md=msg2)
    await search.send(MessageSegment.image(output), at_sender=True)
    await search.send(msg, at_sender=True)
    await session.close()


@play.handle()
async def _play(bot: Bot, matcher: Matcher, ev: MessageEvent, state: State, arg: CommandArg):
    name = ""
    ar = ""
    keywords: list[str] = []
    urls: list[str] = []
    arg = arg.extract_plain_text().strip().split()
    substrings = ["RJ", "rj", "Rj"]
    substring_not_in_arg = True
    for sub in substrings:
        if sub in arg[0]:
            substring_not_in_arg = False
            break

    if not arg or substring_not_in_arg:
        await play.finish("输入的RJ号不符合格式，必须以RJ开头！", at_sender=True)
        return
    rid = arg[0][2:]
    await play.send(f"正在查询音声信息！", at_sender=True)
    session = aiohttp.ClientSession()
    res = await session.get(f"https://api.asmr-200.com/api/workInfo/{rid}", headers=headers, timeout=10)
    r = await res.json()
    try:
        name = r["title"]
    except:
        await play.finish("没有此音声信息或还没有资源", at_sender=True)
        return
    name = r["title"]
    ar = r["name"]
    state["iurl"] = r["mainCoverUrl"]
    img = r["mainCoverUrl"]

    async def process_item(item):
        if item["type"] == "audio":
            keywords.append(item["title"])
            urls.append(item["mediaDownloadUrl"])
        elif item["type"] == "folder":
            for child in item["children"]:
                await process_item(child)

    url = f"https://api.asmr-200.com/api/tracks/{rid}"
    response = await session.get(url, headers=headers, timeout=10)
    result = await response.json()
    for result2 in result:
        await process_item(result2)
    state["keywords"] = keywords
    state["urls"] = urls
    state["ar"] = ar
    state["url"] = f"https://asmr.one/work/RJ{rid}"
    # state["iurl"]=f"https://api.asmr-200.com/api/cover/RJ{rid}.jpg?type=main"
    msg = f'### <div align="center">选择编号</div>\n' \
          f'|<img width="250" src="{img}"/> |{name}  社团名：{ar}|\n' \
          '| :---: | --- |\n'
    for i in range(len(keywords)):
        msg += f'|{str(i + 1)}. | {keywords[i]}|\n'
    msg1 = "请发送序号来获取要听的资源"
    if len(arg) > 1:
        matcher.set_arg("ids", Message(arg[1]))
    else:
        output = await md2img(md=msg)
        await play.send(MessageSegment.image(output), at_sender=True)
        await play.send(msg1, at_sender=True)
    await session.close()


@play.got("ids")
async def _play2(bot: Bot, matcher: Matcher, ev: MessageEvent, state: State, ids: ArgPlainText):
    print(ids)
    try:
        a = int(ids)
    except:
        try_count = state.get("try_count", 1)
        if try_count >= 3:
            await matcher.finish("错误次数过多")
        else:
            state["try_count"] = try_count + 1
        await play.reject("请发送正确的数字")
    i = len(state["urls"])
    ii = 0
    if int(ids) > i:
        ii = i - 1
    else:
        ii = int(ids) - 1
    keywords = state["keywords"][ii]
    urls = state["urls"][ii]
    url = state["url"]
    imgurl = state["iurl"]
    ar = state["ar"]
    m = MessageSegment("music",
                       {"type": "custom", "subtype": "163", "url": url, "audio": urls, "voice": urls, "title": keywords,
                        "content": ar, "image": imgurl})
    await play.send(m)
    return
