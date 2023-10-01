from typing import List
from kirami.log import logger
from kirami import on_command, on_regex
from kirami.matcher import Matcher
from kirami.permission import SUPERUSER
from kirami.depends import Bot, GroupMessageEvent, CommandArg, RegexMatched, ArgStr
from kirami.message import MessageSegment, Message
from kirami.permission import GROUP, GROUP_OWNER, GROUP_ADMIN
from kirami.utils.helpers import extract_plain_text
from kirami.depends import DependsInner
from kirami.utils.jsondata import JsonDict
from kirami.config.path import DATA_DIR
from .config import driver
from .data_source import morning_manager

json_dict = JsonDict(path=DATA_DIR / "morning" / "morning.json", auto_load=True)

__morning_version__ = "v0.3.2"
__morning_usages__ = f'''
[早安] 早安/哦哈哟/おはよう
[晚安] 晚安/哦呀斯密/おやすみ
[我的作息] 看看自己的作息
[群友作息] 看看群友的作息
[早晚安设置] 查看当前配置
'''.strip()


# Good morning/night
morning = on_command("早安", "哦哈哟", "おはよう", permission=GROUP, priority=12)
night = on_command("晚安", "哦呀斯密", "おやすみ", permission=GROUP, priority=12)

# Routine
my_routine = on_command("我的作息", permission=GROUP, priority=12)
group_routine = on_command("群友作息", permission=GROUP, priority=12)

# Settings
configure = on_command("早安设置", "晚安设置", "早晚安设置", permission=GROUP, priority=11, block=True)


@morning.handle()
async def good_morning(bot: Bot, matcher: Matcher, event: GroupMessageEvent, args: CommandArg):
    arg: str = args.extract_plain_text()
    if arg == "帮助":
        await matcher.finish(__morning_usages__)

    uid = event.user_id
    gid = event.group_id
    mem_info = await bot.call_api("get_group_member_info", group_id=gid, user_id=uid)

    sex = mem_info["sex"]
    if sex == "male":
        sex_str = "少年"
    elif sex == "female":
        sex_str = "少女"
    else:
        sex_str = "群友"

    msg = morning_manager.get_morning_msg(str(gid), str(uid), sex_str)
    await matcher.finish(message=msg, at_sender=True)


@night.handle()
async def good_night(bot: Bot, matcher: Matcher, event: GroupMessageEvent, args: CommandArg):
    arg: str = args.extract_plain_text()
    if arg == "帮助":
        await matcher.finish(__morning_usages__)

    uid: int = event.user_id
    gid: int = event.group_id
    mem_info = await bot.call_api("get_group_member_info", group_id=gid, user_id=uid)

    sex = mem_info["sex"]
    if sex == "male":
        sex_str = "少年"
    elif sex == "female":
        sex_str = "少女"
    else:
        sex_str = "群友"

    msg = morning_manager.get_night_msg(str(gid), str(uid), sex_str)
    await matcher.finish(message=msg, at_sender=True)


@my_routine.handle()
async def _(matcher: Matcher, event: GroupMessageEvent):
    gid = str(event.group_id)
    uid = str(event.user_id)

    msg = morning_manager.get_my_routine(gid, uid)
    await matcher.finish(message=msg, at_sender=True)


@group_routine.handle()
async def _(bot: Bot, matcher: Matcher, event: GroupMessageEvent):
    gid = event.group_id
    morning_count, night_count, uid = morning_manager.get_group_routine(str(gid))
    msg: str = f"今天已经有{morning_count}位群友早安了，{night_count}位群友晚安了~"

    if uid:
        mem_info = await bot.call_api("get_group_member_info", group_id=gid, user_id=int(uid))
        nickname: str = mem_info["card"] if mem_info["card"] else mem_info["nickname"]
        msg += f"\n上周睡觉大王是群友：{nickname}，再接再厉～"

    await matcher.finish(MessageSegment.text(msg))


@configure.handle()
async def _(matcher: Matcher, event: GroupMessageEvent):
    gid = str(event.group_id)
    msg = morning_manager.get_group_config()
    await matcher.finish(msg)



# 每日最早晚安时间，重置昨日早晚安计数
@driver.on_startup
async def daily_refresh():
    morning_manager.daily_scheduler()
    logger.info("每日早晚安定时刷新任务已启动！")


# 每周一最晚晚安时间统计部分周数据
@driver.on_startup
async def monday_weekly_night_refresh():
    morning_manager.weekly_night_scheduler()
    logger.info("每周晚安定时刷新任务已启动！")


# 每周一最晚早安时间，统计上周睡眠时间、早安并重置
@driver.on_startup
async def weekly_refresh():
    morning_manager.weekly_sleep_time_scheduler()
    logger.info("每周睡眠时间定时刷新任务已启动！")
