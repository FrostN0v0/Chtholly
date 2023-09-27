from kirami import on_fullmatch, on_time, get_bot, logger
from kirami.typing import Matcher
from kirami.message import Message
from kirami.utils.utils import get_api_data
from kirami.event import GroupMessageEvent
from kirami.config.path import IMAGE_DIR, DATA_DIR
from kirami.utils.downloader import Downloader
from kirami.utils.jsondata import JsonDict
from kirami.utils.resource import Resource
from datetime import date

json_dict = JsonDict(path=DATA_DIR / "daily60s.json", auto_load=True)


daily = on_fullmatch("每日60s", "日报", to_me=True)
daily_sub = on_fullmatch("日报订阅")
daily_delsub = on_fullmatch("日报取消订阅")
daily_download = on_fullmatch("日报更新")
key_name = "group_list"


@daily_sub.handle()
async def add_group(event: GroupMessageEvent, matcher: Matcher):
    if key_name not in json_dict:
        json_dict[key_name] = []  # 如果列表不存在，创建一个空列表
    new_group = event.group_id
    for gid in json_dict[key_name]:
        if new_group == gid:
            await matcher.finish("该群已经订阅了每日60s推送")
        else:
            pass
    json_dict[key_name].append(new_group)
    json_dict.save()
    logger.success(f"群{new_group} 订阅了每日60s推送")
    await matcher.finish("订阅成功！")


@daily_delsub.handle()
async def del_group(event: GroupMessageEvent, matcher: Matcher):
    unsub_group = event.group_id
    for gid in json_dict[key_name]:
        if unsub_group == gid:
            json_dict[key_name].remove(unsub_group)
            json_dict.save()
            logger.success(f"群{unsub_group} 取消了日报推送")
            await matcher.finish("该群订阅的每日60s推送已取消")
        else:
            pass
    await matcher.finish("该群没有订阅每日60s推送")


@daily.handle()
async def daily(matcher: Matcher):
    msg = Message.image(IMAGE_DIR/"daily60s_img"/f'{date.today()}.png')
    await matcher.finish(msg)


@daily_download.handle()
async def rb_download(matcher: Matcher):
    img_name = str(date.today()) + ".png"
    img_today = Resource.image(IMAGE_DIR / "daily60s_img").search(img_name)
    try:
        img = next(img_today)
        if img.name == img_name:
            await matcher.finish("今天的每日60s已是最新")
    except StopIteration:
        response = await get_api_data("https://api.03c3.cn/zb/api.php")
        if response["datatime"] == str(date.today()):
            await Downloader.download_file(response['imageUrl'], IMAGE_DIR / "daily60s_img", file_name=str(date.today()))
        else:
            pass


@on_time("interval", minutes=5)
async def auto_download():
    img_name = str(date.today()) + ".png"
    img_today = Resource.image(IMAGE_DIR / "daily60s_img").search(img_name)
    try:
        img = next(img_today)
    except StopIteration:
        response = await get_api_data("https://api.03c3.cn/zb/api.php")
        if response["datatime"] == str(date.today()):
            await Downloader.download_file(response['imageUrl'], IMAGE_DIR / "daily60s_img", file_name=str(date.today()))
        else:
            pass


@on_time("cron", hour=8, minute=30)
async def push():
    bot = get_bot()
    for gid in json_dict['group_list']:
        msg = Message.image(IMAGE_DIR / "daily60s_img" / f'{date.today()}.png')
        await bot.call_api("send_group_msg", group_id=gid, message=msg)
