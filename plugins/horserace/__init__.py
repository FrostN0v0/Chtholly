import math
import time
import asyncio
from kirami import on_command
from kirami.message import Message, MessageSegment
from kirami.event import GroupMessageEvent, MessageEvent
from kirami.permission import SUPERUSER
from kirami.depends import CommandArg, Bot
from kirami.config.path import FONT_DIR
from kirami.config import bot_config
from kirami.log import logger
from kirami.hook import on_startup
from .start import *
from .race_group import race_group
from .config import Config
from PIL import Image, ImageDraw, ImageFont
from io import BytesIO
from kirami.utils.downloader import Downloader

botname = list(bot_config.nickname)[0]
config = Config.load_config()

events_list = []


# 开场加载
@on_startup
async def init_resourse():
    global events_list
    events_list = await load_dlcs()
    if not (FONT_DIR / "HYWenHei-85W.ttf").exists():
        await Downloader.download_file(
            url='https://raw.githubusercontent.com/FrostN0v0/kirami-plugin-horserace/master/HYWenHei-85W.ttf',
            path=FONT_DIR
        )


RaceNew = on_command("赛马创建", priority=5, block=True)
RaceJoin = on_command("赛马加入", priority=5, block=True)
RaceStart = on_command("赛马开始", priority=5, block=True)
RaceReStart = on_command("赛马重置", priority=5, block=True)
RaceStop = on_command("赛马暂停", priority=5, permission=SUPERUSER, block=True)
RaceClear = on_command("赛马清空", priority=5, permission=SUPERUSER, block=True)
RaceReload = on_command("赛马事件重载", priority=5, permission=SUPERUSER, block=True)
race = {}
forward_msg = []


@RaceNew.handle()
async def _(bot: Bot, event: GroupMessageEvent, arg: CommandArg):
    global race
    group = event.group_id
    try:
        if race[group].start == 0 and time.time() - race[group].time < 300:
            out_msg = f'> 创建赛马比赛失败!\n> 原因:{botname}正在打扫赛马场...\n> 解决方案:等{botname}打扫完...\n> 可以在{str(config.setting_over_time - time.time() + race[group].time)}秒后输入 赛马重置'
            await RaceNew.finish(out_msg)
        elif race[group].start == 1:
            await RaceNew.finish(f"一场赛马正在进行中")
            await RaceNew.finish()
    except KeyError:
        pass
    race[group] = race_group()
    await RaceNew.finish(f'> 创建赛马比赛成功\n> 输入 赛马加入+名字 即可加入赛马')


@RaceJoin.handle()
async def _(bot: Bot, event: GroupMessageEvent, arg: CommandArg):
    global race
    msg = arg.extract_plain_text().strip()
    uid = event.user_id
    group = event.group_id
    player_name = event.sender.card if event.sender.card else event.sender.nickname
    try:
        race[group]
    except KeyError:
        await RaceJoin.finish(f"赛马活动未开始，请输入“赛马创建”开场")
    try:
        if race[group].start == 1 or race[group].start == 2:
            await RaceJoin.finish()
    except KeyError:
        await RaceJoin.finish()
    if race[group].query_of_player() >= config.max_player:
        await RaceJoin.finish(f"> 加入失败\n> 原因:赛马场就那么大，满了满了！")
    if race[group].is_player_in(uid) == True:
        await RaceJoin.finish(f"> 加入失败\n> 原因:您已经加入了赛马场!")
    if msg:
        horse_name = msg
    else:
        await RaceJoin.finish(f"请输入你的马儿名字", at_sender=True)
    race[group].add_player(horse_name, uid, player_name)
    out_msg = f'> 加入赛马成功\n> 赌上马儿性命的一战即将开始!\n> 赛马场位置:{str(race[group].query_of_player())}/{str(config.max_player)}'
    await RaceJoin.finish(out_msg, at_sender=True)


@RaceStart.handle()
async def _(bot: Bot, event: GroupMessageEvent, arg: CommandArg):
    global race
    global events_list
    global forward_msg
    group = event.group_id
    try:
        if race[group].query_of_player() == 0:
            await RaceStart.finish()
    except KeyError:
        await RaceStart.finish()
    try:
        if race[group].start == 0 or race[group].start == 2:
            if len(race[group].player) >= config.min_player:
                race[group].start_change(1)
            else:
                await RaceStart.finish(f'> 开始失败\n> 原因:赛马开局需要最少{str(config.min_player)}人参与', at_sender=True)
        elif race[group].start == 1:
            await RaceStart.finish()
    except KeyError:
        await RaceStart.finish()
    race[group].time = time.time()
    while race[group].start == 1:
        # 显示器初始化
        display = f""
        # 回合数+1
        race[group].round_add()
        # 移除超时buff
        race[group].del_buff_overtime()
        # 马儿全名计算
        race[group].fullname()
        # 回合事件计算
        display += race[group].event_start(events_list)
        # 马儿移动
        race[group].move()
        # 场地显示
        display += race[group].display()
        logger.info(f'事件输出:\n {display}')
        ima = text_to_img(display)
        if config.send_forward_msg:
            forward_msg.append(Message.image(ima))
        else:
            await RaceStart.send(MessageSegment.image(ima))
            await asyncio.sleep(3)
        # 全员失败计算
        if race[group].is_die_all():
            del race[group]
            if config.send_forward_msg:
                msg = [MessageSegment.node(event.user_id, event.sender.nickname, m) for m in forward_msg]
                await bot.call_api("send_group_forward_msg", group_id=event.group_id, messages=msg)
                forward_msg = []
            await asyncio.sleep(2)
            await RaceStart.finish("比赛已结束，鉴定为无马生还")
        # 全员胜利计算
        winer = race[group].is_win_all()
        if winer != f"":
            if config.send_forward_msg:
                msg = [MessageSegment.node(event.user_id, event.sender.nickname, m) for m in forward_msg]
                await bot.call_api("send_group_forward_msg", group_id=event.group_id, messages=msg)
                forward_msg = []
            await asyncio.sleep(1)
            await RaceStart.send(f'> 比赛结束\n> {botname}正在为您生成战报...')
            await asyncio.sleep(2)
            del race[group]
            msg = "比赛已结束，胜者为：" + winer
            await RaceStart.finish(msg)


@RaceReStart.handle()
async def _(bot: Bot, event: GroupMessageEvent, arg: CommandArg):
    global race
    group = event.group_id
    time_key = math.ceil(time.time() - race[group].time)
    if time_key >= config.setting_over_time:
        del race[group]
        await RaceReStart.finish(f'超时{str(config.setting_over_time)}秒，准许重置赛马场')
    await RaceReStart.finish(f'未超时{str(config.setting_over_time)}秒，目前为{str(time_key)}秒，未重置')


@RaceStop.handle()
async def _(bot: Bot, event: GroupMessageEvent, arg: CommandArg):
    global race
    group = event.group_id
    race[group].start_change(2)


@RaceClear.handle()
async def _(bot: Bot, event: GroupMessageEvent, arg: CommandArg):
    global race
    group = event.group_id
    del race[group]


@RaceReload.handle()
async def _(bot: Bot, event: MessageEvent, arg: CommandArg):
    global events_list
    logs = f""
    files = os.listdir(os.path.dirname(__file__) + '/events')
    for file in files:
        try:
            with open(f'{os.path.dirname(__file__)}/events/{file}', "r", encoding="utf-8") as f:
                logger.info(f'加载事件文件：{file}')
                events = deal_events(json.load(f))
                events_list.extend(events)
            logger.info(f"加载 {file} 成功")
            logs += f'加载 {file} 成功\n'
        except:
            logger.info(f"加载 {file} 失败！失败！失败！")
            logs += f"加载 {file} 失败！失败！失败！\n"
    await RaceReload.finish(logs)


def text_to_img(text: str, font_path: str = str(FONT_DIR / "HYWenHei-85W.ttf")) -> BytesIO:
    """
    字转图片
    """
    lines = text.splitlines()
    line_count = len(lines)
    # 读取字体
    font = ImageFont.truetype(font_path, config.pic_font_size)
    # 获取字体的行高
    left, top, width, line_height = font.getbbox("a")
    # 增加行距
    line_height += 3
    # 获取画布需要的高度
    height = line_height * line_count + 20
    # 获取画布需要的宽度
    width = int(max([font.getlength(line) for line in lines])) + 25
    # 字体颜色
    black_color = (0, 0, 0)
    # 生成画布
    image = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    # 按行开画，c是计算写到第几行
    c = 0
    for line in lines:
        draw.text((10, 6 + line_height * c), line, font=font, fill=black_color)
        c += 1
    img_bytes = BytesIO()
    image.save(img_bytes, format="png")
    return img_bytes
