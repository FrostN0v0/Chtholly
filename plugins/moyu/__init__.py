from kirami import on_prefix
from kirami.typing import Matcher
from kirami.message import Message
from kirami import on_time
from kirami import get_bot
from kirami.utils.utils import get_api_data
from kirami.event import GroupMessageEvent
from kirami.config.path import IMAGE_DIR, DATA_DIR
from kirami.utils.downloader import Downloader
from kirami.utils.jsondata import JsonDict
from datetime import date

json_dict = JsonDict(path=DATA_DIR / "moyu.json", auto_load=True)


@on_time("cron", hour=7, minute=55)
async def download():
    response = await get_api_data("https://api.j4u.ink/v1/store/other/proxy/remote/moyu.json")
    await Downloader.download_file(response['data']['moyu_url'], IMAGE_DIR/"moyu_img", file_name=str(date.today()))


@on_time("cron", hour=8, minute=0)
async def push():
    bot = get_bot()
    for gid in json_dict['group_list']:
        msg = Message.image(IMAGE_DIR / "moyu_img" / f'{date.today()}.png')
        await bot.call_api("send_group_msg", group_id=gid, message=msg)


moyu_sub = on_prefix("摸鱼订阅")


@moyu_sub.handle()
async def add_group(event: GroupMessageEvent):
    key_name = "group_list"
    if key_name not in json_dict:
        json_dict[key_name] = []  # 如果列表不存在，创建一个空列表
    new_group = event.group_id
    print(new_group)
    json_dict[key_name].append(new_group)
    json_dict.save()


@on_prefix("moyu", "摸鱼", "摸鱼日历")
async def moyu(matcher: Matcher):
    msg = Message.image(IMAGE_DIR/"moyu_img"/f'{date.today()}.png')
    await matcher.finish(msg)
