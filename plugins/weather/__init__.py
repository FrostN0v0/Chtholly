from kirami import on_keyword, on_command, get_bot
from kirami.log import logger
from kirami.matcher import Matcher
from kirami.event import MessageEvent, GroupMessageEvent
from kirami.permission import GROUP
from kirami.message import MessageSegment
from kirami.depends import CommandArg
from kirami.hook import on_startup
from kirami.config.path import DATA_DIR
from httpx import AsyncClient
from kirami.utils.jsondata import JsonDict
from kirami.utils import scheduler, new_dir

from .render_pic import render
from .weather_data import Weather, ConfigError, CityNotFoundError
from .config import DEBUG, QWEATHER_APIKEY, QWEATHER_APITYPE, Config

json_dict = JsonDict(path=DATA_DIR / "weather" / "weather.json", auto_load=True)
gsub_data = JsonDict(path=DATA_DIR / "weather" / "gsub_weather.json", auto_load=True)

if DEBUG:
    logger.debug("将会保存图片到 weather.png")

weather = on_keyword("天气", priority=10)
sub_weather = on_command("天气订阅", priority=5)
gsub_weather = on_command("群天气订阅", priority=5, permission=GROUP)
lookup_p_sub = on_command("查询天气订阅", priority=5)
lookup_g_sub = on_command("查询群天气订阅", priority=5, permission=GROUP)
del_p_sub = on_command("删除天气订阅", priority=5)
del_g_sub = on_command("删除群天气订阅", priority=5, permission=GROUP)


@on_startup
async def init_weather():
    for uid in json_dict:
        for i, data_dict in enumerate(json_dict[uid]):
            location = data_dict['location']
            hour = data_dict['hour']
            scheduler.add_job(push_privite_msg, 'cron', hour=hour, id=f"weather_{uid}_{location}_{hour}",
                              kwargs={'uid': uid, 'city': location})
    for gid in gsub_data:
        for uid in gsub_data[gid]:
            for i, data_dict in enumerate(gsub_data[gid][uid]):
                location = data_dict['city']
                hour = data_dict['hour']
                scheduler.add_job(push_group_msg, 'cron', hour=hour, id=f"weather_{gid}_{uid}_{location}_{hour}",
                                  kwargs={'uid': uid, 'gid': gid, 'city': location})
    logger.info("载入天气订阅定时任务完成")
    if not (DATA_DIR / "weather").exists():
        new_dir(DATA_DIR / "weather")


async def push_privite_msg(uid: str, city: str):
    bot = get_bot()
    w_data = Weather(city_name=city, api_key=QWEATHER_APIKEY, api_type=QWEATHER_APITYPE)
    await w_data.load_data()
    img = await render(w_data)
    msg = MessageSegment.text(f"您订阅的{city}实时天气送达啦~\n")
    msg += MessageSegment.image(img)
    await bot.call_api('send_private_msg', user_id=uid, message=msg)


async def push_group_msg(uid: str, gid: str, city: str):
    bot = get_bot()
    w_data = Weather(city_name=city, api_key=QWEATHER_APIKEY, api_type=QWEATHER_APITYPE)
    await w_data.load_data()
    img = await render(w_data)
    msg = MessageSegment.at(uid)
    msg += MessageSegment.text(f"您订阅的{city}实时天气送达啦~\n")
    msg += MessageSegment.image(img)
    await bot.call_api('send_group_msg', group_id=gid, message=msg)


@weather.handle()
async def _(matcher: Matcher, event: MessageEvent):
    if not (QWEATHER_APIKEY and QWEATHER_APITYPE):
        raise ConfigError("请设置 qweather_apikey 和 qweather_apitype")

    city = ""
    if args := event.get_plaintext().split("天气"):
        city = args[0].strip() or args[1].strip()
        if not city:
            await weather.finish("地点是...空气吗?? >_<")

        # 判断指令前后是否都有内容，如果是则结束，否则跳过。
        if (args[0].strip() == "") == (args[1].strip() == ""):
            await weather.finish()
    w_data = Weather(city_name=city, api_key=QWEATHER_APIKEY, api_type=QWEATHER_APITYPE)
    try:
        await w_data.load_data()
    except CityNotFoundError:
        matcher.block = False
        await weather.finish()

    img = await render(w_data)

    if DEBUG:
        debug_save_img(img)

    await weather.finish(MessageSegment.image(img))


@sub_weather.handle()
async def sub(event: MessageEvent, arg: CommandArg, matcher: Matcher):
    args = arg.extract_plain_text().split()
    res = await search_city(str(args[0]))
    res = res.json()
    if res["code"] != "200":
        await matcher.finish("未获取到城市信息，请检查命令格式或城市名是否正确")
    city_name = res["location"][0]["name"]
    uid = str(event.user_id)
    if uid not in json_dict:
        json_dict[uid] = []
    new_data = {'location': city_name, 'hour': 8}
    if len(args) > 1:
        if 24 > int(args[1]) >= 0:
            new_data = {'location': city_name, 'hour': args[1]}
    if new_data not in json_dict[uid]:
        json_dict[uid].append(new_data)
    else:
        await matcher.finish("请勿重复订阅喵~")
    json_dict.save()
    if len(args) > 1:
        scheduler.add_job(push_privite_msg, 'cron', hour=args[1], id=f"weather_{uid}_{city_name}_{args[1]}",
                          kwargs={'uid': uid, 'city': city_name})
        await matcher.finish(f"订阅{city_name}天气推送成功，将于每日{args[1]}时推送实时天气")
    else:
        scheduler.add_job(push_privite_msg, 'cron', hour=8, id=f"weather_{uid}_{city_name}_8",
                          kwargs={'uid': uid, 'city': city_name})
        await matcher.finish(f"订阅{city_name}天气推送成功，将于每日8时推送实时天气")


@gsub_weather.handle()
async def gsub(event: GroupMessageEvent, arg: CommandArg, matcher: Matcher):
    args = arg.extract_plain_text().split()
    gid = str(event.group_id)
    uid = str(event.user_id)
    res = await search_city(str(args[0]))
    res = res.json()
    if res["code"] != "200":
        await matcher.finish("未获取到城市信息，请检查命令格式或城市名是否正确")
    city_name = res["location"][0]["name"]
    data_dict = {"city": city_name, "hour": 8}
    if len(args) > 1:
        if 24 > int(args[1]) >= 0:
            data_dict = {"city": city_name, "hour": args[1]}
    if gid not in gsub_data:
        gsub_data[gid] = {}
        if uid not in gsub_data[gid]:
            gsub_data[gid][uid] = []
    if data_dict not in gsub_data[gid][uid]:
        gsub_data[gid][uid].append(data_dict)
    else:
        await matcher.finish("请勿重复订阅喵~")
    gsub_data.save()
    msg = MessageSegment.text("群用户") + MessageSegment.at(event.user_id)
    if len(args) > 1:
        scheduler.add_job(push_group_msg, 'cron', hour=args[1], id=f"weather_{gid}_{uid}_{city_name}_{args[1]}",
                          kwargs={'uid': uid, 'gid': gid, 'city': city_name})
        msg += MessageSegment.text(f"订阅{city_name}天气推送成功，将于每日{args[1]}时推送实时天气")
    else:
        scheduler.add_job(push_group_msg, 'cron', hour=8, id=f"weather_{gid}_{uid}_{city_name}_8",
                          kwargs={'uid': uid, 'gid': gid, 'city': city_name})
        msg += MessageSegment.text(f"订阅{city_name}天气推送成功，将于每日8时推送实时天气")
    await matcher.finish(message=msg)


@lookup_p_sub.handle()
async def send_p_subs(event: MessageEvent, matcher: Matcher):
    uid = str(event.user_id)
    msg = MessageSegment.at(uid)
    msg += '您的天气推送订阅列表如下喵~：\n'
    if uid in json_dict:
        for i, data_dict in enumerate(json_dict[uid]):
            location = data_dict['location']
            hour = data_dict['hour']
            msg += f"{i+1}.城市：{location}  推送时间：{hour}\n"
    await matcher.finish(message=msg)


@lookup_g_sub.handle()
async def send_g_subs(event: GroupMessageEvent, matcher: Matcher):
    uid = str(event.user_id)
    gid = str(event.group_id)
    msg = MessageSegment.at(uid)
    msg += '您在本群的天气推送订阅列表如下喵~：\n'
    if gid in gsub_data:
        if uid in gsub_data[gid]:
            for i, data_dict in enumerate(gsub_data[gid][uid]):
                location = data_dict['city']
                hour = data_dict['hour']
                msg += f"{i+1}.城市：{location}  推送时间：{hour}\n"
    await matcher.finish(message=msg)


@del_p_sub.handle()
async def del_p_subs(event: MessageEvent, matcher: Matcher, arg: CommandArg):
    args = arg.extract_plain_text().split()
    uid = str(event.user_id)
    msg = "删除订阅成功喵~\n本次删除天气订阅如下：\n"
    if len(args) == 1:
        sub_id = int(args[0])
        if sub_id <= len(json_dict[uid]):
            location = json_dict[uid][sub_id-1]['location']
            hour = json_dict[uid][sub_id-1]['hour']
            msg += f"{sub_id}.城市：{location}  推送时间：{hour}\n"
            scheduler.remove_job(job_id=f"weather_{uid}_{location}_{hour}")
            del json_dict[uid][sub_id-1]
    json_dict.save()
    msg2 = MessageSegment.at(uid)
    msg2 += '您现在的天气推送订阅列表如下喵~：\n'
    if uid in json_dict:
        for i, data_dict in enumerate(json_dict[uid]):
            location = data_dict['location']
            hour = data_dict['hour']
            msg2 += f"{i + 1}.城市：{location}  推送时间：{hour}\n"
    await matcher.send(message=msg)
    await matcher.finish(message=msg2)


@del_g_sub.handle()
async def del_g_subs(event: GroupMessageEvent, matcher: Matcher, arg: CommandArg):
    args = arg.extract_plain_text().split()
    uid = str(event.user_id)
    gid = str(event.group_id)
    msg = MessageSegment.at(uid)
    msg += "删除订阅成功喵~\n本次删除群天气订阅如下：\n"
    if len(args) == 1:
        sub_id = int(args[0])
        if sub_id <= len(gsub_data[gid][uid]):
            location = gsub_data[gid][uid][sub_id-1]['city']
            hour = gsub_data[gid][uid][sub_id-1]['hour']
            msg += f"{sub_id}.城市：{location}  推送时间：{hour}\n"
            scheduler.remove_job(job_id=f'weather_{gid}_{uid}_{location}_{hour}')
            del gsub_data[gid][uid][sub_id-1]
    gsub_data.save()
    msg2 = MessageSegment.at(uid)
    msg2 += '您现在在本群的天气推送订阅列表如下喵~：\n'
    if gid in gsub_data:
        if uid in gsub_data[gid]:
            for i, data_dict in enumerate(gsub_data[gid][uid]):
                location = data_dict['city']
                hour = data_dict['hour']
                msg2 += f"{i+1}.城市：{location}  推送时间：{hour}\n"
    await matcher.send(message=msg)
    await matcher.finish(message=msg2)


def debug_save_img(img: bytes) -> None:
    from io import BytesIO

    from PIL import Image

    logger.debug("保存图片到 weather.png")
    a = Image.open(BytesIO(img))
    a.save("weather.png", format="PNG")


async def search_city(city: str):
    client = AsyncClient()
    url = "https://geoapi.qweather.com/v2/city/lookup"
    params = {"location": city, "key": QWEATHER_APIKEY, "number": 1}
    res = await client.get(url, params=params)
    return res
