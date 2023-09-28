from kirami import on_fullmatch, on_time, get_bot, logger
from kirami.typing import Matcher
from kirami.message import Message
from kirami.utils.utils import get_api_data
from kirami.event import GroupMessageEvent
from kirami.config.path import IMAGE_DIR, DATA_DIR
from kirami.utils.downloader import Downloader
from kirami.utils.jsondata import JsonDict
from kirami.hook import on_startup
from kirami.utils.utils import new_dir
from datetime import date

json_dict = JsonDict(path=DATA_DIR / "moyu.json", auto_load=True)

moyu = on_fullmatch("moyu", "摸鱼", "摸鱼日历", to_me=True)
moyu_sub = on_fullmatch("摸鱼订阅")
moyu_download = on_fullmatch("摸鱼更新")
moyu_delsub = on_fullmatch("摸鱼订阅取消")
key_name = "group_list"


@on_startup
async def check_dir():
    if not (IMAGE_DIR / "moyu_img").exists():
        new_dir(IMAGE_DIR / "moyu_img")

@moyu_sub.handle()
async def add_group(event: GroupMessageEvent, matcher: Matcher):
    if key_name not in json_dict:
        json_dict[key_name] = []  # 如果列表不存在，创建一个空列表
    new_group = event.group_id
    for gid in json_dict[key_name]:
        if new_group == gid:
            await matcher.finish("该群已经订阅了摸鱼日历推送")
        else:
            pass
    json_dict[key_name].append(new_group)
    json_dict.save()
    logger.success(f"群{new_group} 订阅了摸鱼日历推送")
    await matcher.finish("订阅成功！")


@moyu_delsub.handle()
async def del_group(event: GroupMessageEvent, matcher: Matcher):
    unsub_group = event.group_id
    for gid in json_dict[key_name]:
        if unsub_group == gid:
            json_dict[key_name].remove(unsub_group)
            json_dict.save()
            logger.success(f"群{unsub_group} 取消了摸鱼日历推送")
            await matcher.finish("该群订阅摸鱼日历推送已取消")
        else:
            pass
    await matcher.finish("该群没有订阅摸鱼日历推送")


@moyu.handle()
async def moyu(matcher: Matcher):
    msg = Message.image(IMAGE_DIR/"moyu_img"/f'{date.today()}.png')
    await matcher.finish(msg)


@on_time("cron", hour=8, minute=0)
async def push():
    bot = get_bot()
    for gid in json_dict['group_list']:
        msg = Message.image(IMAGE_DIR / "moyu_img" / f'{date.today()}.png')
        await bot.call_api("send_group_msg", group_id=gid, message=msg)


@on_time("cron", hour=7, minute=55)
async def auto_download():
    response = await get_api_data("https://api.j4u.ink/v1/store/other/proxy/remote/moyu.json")
    await Downloader.download_file(response['data']['moyu_url'], IMAGE_DIR/"moyu_img", file_name=str(date.today()))


@moyu_download.handle()
async def download():
    response = await get_api_data("https://api.j4u.ink/v1/store/other/proxy/remote/moyu.json")
    await Downloader.download_file(response['data']['moyu_url'], IMAGE_DIR/"moyu_img", file_name=str(date.today()))
