import re
import random
import datetime
from typing import Set, Dict, Any, Union, List

from kirami import on_regex, Bot
from kirami.depends import RegexDict
from kirami.permission import SUPERUSER
from kirami.event import GroupMessageEvent
from kirami.exception import ActionFailed
from kirami.permission import GROUP, GROUP_OWNER, GROUP_ADMIN
from kirami.message import Message

from .config import Config
from .record import get_group_record, save_group_record, construct_waifu_msg, clear_group_record, \
    construct_change_waifu_msg

__plugin_name__ = '今日老婆'
__plugin_version__ = '0.1.5'
"""
        "指令表：\n"
        "▶ 今日老婆\n"
        "  ▷ 范围：群聊\n"
        "  ▷ 介绍：随机抽取群友作为老婆，返回头像和昵称。当天已经抽取过回复相同老婆\n"
        "▶ 换老婆\n"
        "  ▷ 范围：群聊\n"
        "  ▷ 介绍：重新抽取老婆\n"
        "▶ (刷新/重置)今日老婆 | (刷新/重置)自定义别名\n"
        "  ▷ 权限：主人\n"
        "  ▷ 范围：群聊\n"
        "  ▷ 介绍：清空今日本群老婆数据\n"
        "▶ (开启/关闭)换老婆\n"
        "  ▷ 权限：主人/群主/管理员\n"
        "  ▷ 范围：群聊\n"
        "  ▷ 介绍：开启/关闭本群换老婆功能\n"
        "▶ 设置换老婆次数 <N>\n"
        "  ▷ 权限：主人/群主/管理员\n"
        "  ▷ 范围：群聊\n"
        "  ▷ 介绍：设置本群换老婆最大次数\n"
        "  ▷ 参数：\n"
        "    ▷ N：指定整数次数
        """

waifu_config = Config.load_config()

plugin_aliases: List[str] = waifu_config.today_waifu_aliases
ban_id: Set[int] = waifu_config.today_waifu_ban_id_list
default_allow_change_waifu: bool = waifu_config.today_waifu_default_change_waifu
default_limit_times: int = waifu_config.today_waifu_default_limit_times
today_waifu_superuser_opt: bool = waifu_config.today_waifu_superuser_opt
if today_waifu_superuser_opt:
    permission_opt = SUPERUSER
else:
    permission_opt = SUPERUSER | GROUP_OWNER | GROUP_ADMIN

# 正则匹配插件名与别名的字符串
PatternStr = '|'.join([__plugin_name__, ] + plugin_aliases)

# 响应器主体
today_waifu = on_regex(
    rf'^\s*({PatternStr})\s*$',
    flags=re.S,
    permission=GROUP | SUPERUSER,
    priority=7,
    block=True
)

# 刷新所在群全部记录
today_waifu_refresh = on_regex(
    rf"^\s*(刷新|重置)(?P<name>{PatternStr})\s*$",
    permission=SUPERUSER,
    priority=7,
    block=True
)

# 换老婆
today_waifu_change = on_regex(
    r'^\s*换老婆\s*$',
    flags=re.S,
    permission=GROUP | SUPERUSER,
    priority=7,
    block=True
)

# 设置所在群换老婆最大次数
today_waifu_set_limit_times = on_regex(
    rf"^\s*设置换老婆次数\s*(?P<times>\d+)\s*$",
    permission=permission_opt,
    priority=7,
    block=True
)

today_waifu_set_allow_change = on_regex(
    rf"^\s*(?P<val>开启换老婆|关闭换老婆)\s*$",
    permission=permission_opt,
    priority=7,
    block=True
)


@today_waifu_set_allow_change.handle()
async def _(event: GroupMessageEvent, val: Dict[str, Any] = RegexDict()):
    gid = str(event.group_id)
    group_record: Dict[str, Union[bool, Dict[str, Dict[str, int]]]] = get_group_record(gid)  # 获取本群记录字典
    val: str = val.get('val', '').strip()
    if val == '开启换老婆':
        group_record['allow_change_waifu'] = True
    elif val == '关闭换老婆':
        group_record['allow_change_waifu'] = False
    else:
        await today_waifu_set_allow_change.finish()
    save_group_record(gid, group_record)
    await today_waifu_set_allow_change.finish(f'本群设置为{val}')


@today_waifu_set_limit_times.handle()
async def _(event: GroupMessageEvent, times: Dict[str, Any] = RegexDict()):
    limit_times: str = times.get('times', str(default_limit_times)).strip()
    try:
        limit_times_num = int(limit_times)
    except ValueError:
        await today_waifu_set_limit_times.finish('换老婆次数应为整数')
        return
    gid = str(event.group_id)
    group_record: Dict[str, Union[int, Dict[str, Dict[str, int]]]] = get_group_record(gid)  # 获取本群记录字典
    group_record['limit_times'] = limit_times_num
    save_group_record(gid, group_record)
    await today_waifu_set_limit_times.finish(f'已将本群换老婆次数设置为{limit_times_num}次')


@today_waifu_change.handle()
async def _(bot: Bot, event: GroupMessageEvent):
    gid = str(event.group_id)
    uid = str(event.user_id)
    today = str(datetime.date.today())
    group_record: Dict[str, Union[int, bool, Dict[str, Dict[str, int]]]] = get_group_record(gid)  # 获取本群记录字典
    limit_times: int = group_record.setdefault('limit_times', default_limit_times)
    allow_change_waifu: bool = group_record.setdefault('allow_change_waifu', default_allow_change_waifu)
    if today not in group_record.keys() or uid not in group_record[today].keys():
        await today_waifu_change.finish('换老婆前请先娶个老婆哦，渣男', at_sender=True)
    if not allow_change_waifu:
        await today_waifu_change.finish('请专一的对待自己的老婆哦', at_sender=True)
    group_today_record: Dict[str, Dict[str, int]] = group_record[today]  # 获取本群今日字典
    old_waifu_id: int = group_today_record[uid].get('waifu_id', 1234567)
    old_times: int = group_today_record[uid].setdefault('times', 0)
    if old_times >= limit_times or old_waifu_id == int(bot.self_id):
        new_waifu_id = -1
        old_times = limit_times
    else:
        all_member: list = await bot.get_group_member_list(group_id=event.group_id)
        id_set: Set[int] = set(i['user_id'] for i in all_member) - set(
            i['waifu_id'] for i in group_today_record.values()) - ban_id
        id_set.discard(int(uid))
        if id_set:
            new_waifu_id: int = random.choice(list(id_set))
        else:
            # 如果剩余群员列表为空，默认机器人作为老婆
            new_waifu_id: int = int(bot.self_id)
    group_today_record[uid] = {
        'waifu_id': new_waifu_id,
        'times': old_times + 1
    }
    save_group_record(gid, group_record)
    try:
        member_info = await bot.get_group_member_info(group_id=event.group_id, user_id=new_waifu_id)
    except ActionFailed:
        # 群员已经退群情况
        member_info = {}
    message: Message = await construct_change_waifu_msg(member_info, new_waifu_id, int(bot.self_id), old_times,
                                                        limit_times)
    await today_waifu_change.finish(message, at_sender=True)


@today_waifu.handle()
async def _(bot: Bot, event: GroupMessageEvent):
    gid = str(event.group_id)
    uid = str(event.user_id)
    today = str(datetime.date.today())
    group_record: Dict[str, Union[int, bool, Dict[str, Dict[str, int]]]] = get_group_record(gid)  # 获取本群记录字典
    limit_times: int = group_record.setdefault('limit_times', default_limit_times)
    allow_change_waifu: bool = group_record.setdefault('allow_change_waifu', default_allow_change_waifu)
    save = False  # 保存标记，是否将记录写入到本地文件
    is_first: bool  # 是否已经存在老婆标记
    waifu_id: int  # 老婆id
    if today not in group_record.keys():
        # 如果不存在今天的记录，清空本群记录字典，并添加今天的记录，保存标记置为真
        group_record.clear()
        group_record['limit_times'] = limit_times
        group_record['allow_change_waifu'] = allow_change_waifu
        group_record[today] = {}
        save = True
    group_today_record: Dict[str, Dict[str, int]] = group_record[today]  # 获取本群今日字典
    if uid in group_today_record.keys():
        # 如果用户在今天已经有老婆记录，记录已经存在老婆 同时 记录老婆id
        waifu_id: int = group_today_record[uid].get('waifu_id', 1234567)
        is_first = False
    else:
        # 如果用户在今天无老婆记录，随机从群友中抓取一位作为老婆，同时保证别人的老婆不会被抓（NTR禁止）
        all_member: list = await bot.get_group_member_list(group_id=event.group_id)
        id_set: Set[int] = set(i['user_id'] for i in all_member) - set(
            i['waifu_id'] for i in group_today_record.values()) - ban_id
        id_set.discard(int(uid))
        if id_set:
            waifu_id: int = random.choice(list(id_set))
        else:
            # 如果剩余群员列表为空，默认机器人作为老婆
            waifu_id: int = int(bot.self_id)
        group_today_record[uid] = {
            'waifu_id': waifu_id,
            'times': 0,
        }
        save = True
        is_first = True
    if save:
        save_group_record(gid, group_record)
    try:
        member_info = await bot.get_group_member_info(group_id=event.group_id, user_id=waifu_id)
    except ActionFailed:
        # 群员已经退群情况
        member_info = {}
    message: Message = await construct_waifu_msg(member_info, waifu_id, int(bot.self_id), is_first)
    await today_waifu.finish(message, at_sender=True)


@today_waifu_refresh.handle()
async def _(event: GroupMessageEvent, name: Dict[str, Any] = RegexDict()):
    plugin_name: str = name.get('name', __plugin_name__).strip()
    clear_group_record(str(event.group_id))
    await today_waifu_refresh.finish(f"{plugin_name}已刷新！")
