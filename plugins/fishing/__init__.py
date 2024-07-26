import base64
import random
from io import BytesIO

from kirami import on_command, Bot

from kirami.event import GroupMessageEvent
from kirami.message import MessageSegment
from kirami.log import logger

import asyncio

from .config import config
from .data_source import (
    choice,
    get_cd,
    get_stats,
    parse_empty_data,
    get_fish_weight_rank,
    get_balance_rank,
)
from .image_handle import handbook_card_image, get_pic


fishing = on_command("fishing", "钓鱼", "🎣", priority=15)
stats = on_command("stats", "钓鱼统计", "钓鱼统计信息", priority=15)
rank = on_command("rank", "钓鱼排行", "钓鱼排名", priority=15)
balance = on_command("balance", "鱼干", "钓鱼余额", priority=15)
handbook = on_command("handbook", "鱼鉴", "钓鱼手册", priority=15)


@fishing.handle()
async def _(event: GroupMessageEvent):
    cd_time = await get_cd(str(event.group_id), str(event.user_id))
    if cd_time is None:
        if config.fish_empty_chance < random.random():
            result = await fishing_handler(event)
            await fishing.finish(result, at_sender=True)
        else:
            fish_time = get_fish_time()
            await parse_empty_data(str(event.group_id), str(event.user_id))
            await fishing.send(f"正在钓鱼…请耐心等待上钩")
            await asyncio.sleep(fish_time)
            await fishing.finish("杂鱼♥杂鱼♥，第一次钓鱼就没能上钩呢~真是杂鱼♥", at_sender=True)
    elif cd_time >= config.fishing_limit:
        if config.fish_empty_chance < random.random():
            result = await fishing_handler(event)
            await fishing.finish(result, at_sender=True)
        else:
            fish_time = get_fish_time()
            await parse_empty_data(str(event.group_id), str(event.user_id))
            await fishing.send(f"正在钓鱼…请耐心等待上钩")
            await asyncio.sleep(fish_time)
            await fishing.finish("你猛地一提钩,但发现钩子上空空如也，空军乃钓家常事，休息一下吧", at_sender=True)
    else:
        can_fish_time = config.fishing_limit - cd_time
        await fishing.finish(f"你刚刚已经钓过了, 休息一下吧, 还有{can_fish_time // 60}分钟{can_fish_time % 60:02d}秒才能钓鱼", at_sender=True)


@stats.handle()
async def _(event: GroupMessageEvent):
    msg = await get_stats(str(event.group_id), str(event.user_id))
    await stats.finish(msg)


async def fishing_handler(event: GroupMessageEvent):
    fish_detail, fish_weight, selected_rarity, fish_vault = await choice(str(event.group_id), str(event.user_id))
    logger.info(fish_detail['display-name'], fish_weight, selected_rarity)
    await fishing.send(f"正在钓鱼…请耐心等待上钩")
    fish_time = get_fish_time()
    await asyncio.sleep(fish_time)
    result = f"你钓到了一条【{fish_detail['display-name']}】\n"
    fish_pic_name = f"{fish_detail['display-name']}.png"
    fish_pic = get_pic(fish_pic_name, False)
    if fish_pic:
        buf = BytesIO()
        fish_pic = fish_pic.convert('RGBA')
        fish_pic.save(buf, format='PNG')
        base64_str = f'base64://{base64.b64encode(buf.getvalue()).decode()}'
        result += MessageSegment.image(base64_str)
    result += (f"品质【{selected_rarity['display-star']}】\n"
               f"重 【{fish_weight}】Kg\n"
               f"价值 【{fish_vault}】{config.fishing_coin_name}\n"
               )
    try:
        if fish_detail['msg']:
            result += f"{fish_detail['msg']}\n"
    except KeyError:
        logger.warning("no msg keys")
    result += "收获满满~"
    return result


def get_fish_time():
    fish_time = random.randint(config.fishing_time_radius[0], config.fishing_time_radius[1])
    return fish_time


@rank.handle()
async def _(event: GroupMessageEvent, bot: Bot):
    msg = await get_fish_weight_rank(bot, str(event.group_id), str(event.user_id))
    await rank.finish(msg)


@balance.handle()
async def _(event: GroupMessageEvent, bot: Bot):
    msg = await get_balance_rank(bot, str(event.group_id), str(event.user_id))
    await balance.finish(msg)


@handbook.handle()
async def _(event: GroupMessageEvent):
    msg = MessageSegment.image(await handbook_card_image(str(event.group_id), str(event.user_id)))
    await handbook.finish(msg)
