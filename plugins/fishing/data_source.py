import random
import time
from typing import List

import numpy as np

from datetime import datetime
from collections import Counter
from .config import config
from kirami.utils.jsondata import JsonDict
from kirami import Bot
from .model import FishHistory, FishUserData

fishing_coin_name = config.fishing_coin_name

fish_data = JsonDict(path=config.fish_dir / "fishes.json", auto_load=True)


async def get_cd(gid: str, uid: str) -> int:
    """获取用户上次钓鱼时间"""
    time_now = int(time.time())
    if fish_record := FishHistory.find(FishHistory.gid == gid, FishHistory.user_id == uid).desc('fish_stamp'):
        async for fi in fish_record:
            last_fish: FishHistory = fi
            last_stamp = dict(last_fish).get("fish_stamp")
            return time_now - last_stamp


async def can_fishing(cd_now: int) -> bool:
    """判断是否可以钓鱼"""
    return True if not cd_now else cd_now > config.fishing_limit


async def choice(gid: str, uid: str):
    """从json文件中随机获取鱼及其信息"""
    rarity_list = fish_data.get("rarity-list")
    rarity_names = list(rarity_list.keys())
    chances = np.array([rarity_list[name]['chance'] for name in rarity_names])
    # 使用numpy根据权重随机一种稀有度
    selected_rarity = np.random.choice(rarity_names, p=chances / chances.sum())
    selected_rarity_name = {selected_rarity: rarity_list[selected_rarity]}
    rarity_name = list(selected_rarity_name.keys())[0]
    # 从该稀有度的鱼列表中随机一种鱼
    fish_list = fish_data.get("fish-list")[rarity_name]
    fish_detail = random.choice(list(fish_list.values()))
    # 在该鱼的重量范围内随机生成重量（保留两位小数）
    fish_weight = round(random.uniform(fish_detail["weight-min"], fish_detail["weight-max"]), 2)
    fish_vault = fish_weight * config.fish_multiplier + rarity_list[selected_rarity]["additional-price"]
    fish_vault = round(fish_vault, 2)
    # 存入背包数据和钓鱼记录
    fish_record = FishHistory(
        gid=gid,
        user_id=uid,
        fish_name=fish_detail["display-name"],
        weight=fish_weight,
        fish_stamp=int(time.time())
    )
    if fish_user_data := await FishUserData.find(FishUserData.gid == gid, FishUserData.user_id == uid).get():
        fish_user_data.coin += fish_vault
        await fish_user_data.update()
    else:
        fish_user_data = FishUserData(
            gid=gid,
            user_id=uid,
            coin=fish_vault,
            register_time=datetime.now()
        )
        await fish_user_data.save()
    await fish_user_data.save()
    await fish_record.save()

    return fish_detail, fish_weight, rarity_list[selected_rarity], fish_vault


async def get_stats(gid: str, uid: str) -> str:
    """查询钓鱼信息-历史最重的鱼-钓到最多的鱼-钓鱼次数-钓鱼注册时间-货币余额"""
    result = '🐟钓鱼生涯🐟\n'
    if fish_record := FishHistory.find(FishHistory.gid == gid, FishHistory.user_id == uid):
        fish_catch_list = []
        fish_times = await fish_record.count()
        result += f"钓鱼次数:{fish_times}\n"
        async for fi in fish_record:
            last_fish_name = dict(fi).get("fish_name")
            fish_catch_list.append(last_fish_name)
        fish_counts = Counter(fish_catch_list).most_common(1)
        result += f"钓到最多的鱼:{fish_counts[0][0]}|钓到次数:{fish_counts[0][1]}\n"
        fish_weight = fish_record.desc("weight")
        async for wei in fish_weight:
            heaviest_fish = dict(wei)
            result += f"历史最重的鱼:{heaviest_fish['fish_name']}|重量:{heaviest_fish['weight']}Kg\n"
            break
    if fish_user_data := await FishUserData.find(FishUserData.gid == gid, FishUserData.user_id == uid).get():
        reg_time = fish_user_data.register_time.strftime("%Y年%m月%d日 %H:%M:%S")
        result += f"钓鱼注册时间:{reg_time}\n"
        result += f"余额:{fish_user_data.coin:.2f}{config.fishing_coin_name}\n"
    return result


async def parse_empty_data(gid: str, uid: str):
    fish_record = FishHistory(
        gid=gid,
        user_id=uid,
        fish_name="空军了",
        weight=0,
        fish_stamp=int(time.time())
    )
    await fish_record.save()


async def get_fish_weight_rank(bot: Bot, gid: str, uid: str) -> str:
    """获取钓鱼排行榜"""
    result = ''
    fish_record = FishHistory.find(FishHistory.gid == gid).desc('weight')
    weight_rank_list = []
    user_id_list = []
    async for fi in fish_record:
        user_id_list.append(dict(fi).get("user_id"))
        user_id_list = list(set(user_id_list))
    for u in user_id_list:
        u_heaviest_fish = await FishHistory.find(FishHistory.gid == gid, FishHistory.user_id == u).desc('weight')
        weight_rank_list.append(dict(u_heaviest_fish[0]))
    data_sorted_desc = sorted(weight_rank_list, key=lambda x: x['weight'], reverse=True)
    if len(data_sorted_desc) >= 5:
        for all_rank in range(5):
            nickname = await get_nickname(bot, data_sorted_desc[all_rank].get("user_id"), gid)
            result += (
                f"第{all_rank + 1}名:{nickname},{data_sorted_desc[all_rank].get('fish_name')}|"
                f"{data_sorted_desc[all_rank].get('weight')}Kg\n"
            )
        for u_rank in range(len(data_sorted_desc)):
            if data_sorted_desc[u_rank].get("user_id") == uid:
                result += (f"您的钓鱼排名为{u_rank + 1}:{data_sorted_desc[u_rank].get('fish_name')}|"
                           f"{data_sorted_desc[u_rank].get('weight')}Kg\n"
                           )
    elif 5 > len(data_sorted_desc) > 0:
        for all_rank in range(len(data_sorted_desc)):
            nickname = await get_nickname(bot, data_sorted_desc[all_rank].get("user_id"), gid)
            result += (
                f"第{all_rank + 1}名:{nickname},{data_sorted_desc[all_rank].get('fish_name')}|"
                f"{data_sorted_desc[all_rank].get('weight')}Kg\n"
            )
        for u_rank in range(len(data_sorted_desc)):
            if data_sorted_desc[u_rank].get("user_id") == uid:
                result += (f"您的钓鱼排名为{u_rank + 1}:{data_sorted_desc[u_rank].get('fish_name')}|"
                           f"{data_sorted_desc[u_rank].get('weight')}Kg\n"
                           )
    else:
        result = "暂无钓鱼记录"
    return result


async def get_balance_rank(bot: Bot, gid: str, uid: str):
    """获取余额排行榜"""
    result = ''
    balance_list = []
    if fish_user_data := FishUserData.find(FishUserData.gid == gid).desc('coin'):
        async for fi in fish_user_data:
            balance_list.append(dict(fi))
        if len(balance_list) >= 5:
            for all_rank in range(5):
                nickname = await get_nickname(bot, balance_list[all_rank].get("user_id"), gid)
                result += (
                    f"第{all_rank + 1}名:{nickname},{balance_list[all_rank].get('coin'):.2f}{config.fishing_coin_name}\n"
                )
            for u_rank in range(len(balance_list)):
                if balance_list[u_rank].get("user_id") == uid:
                    result += (f"您的余额排名为{u_rank + 1}:【{balance_list[u_rank].get('coin'):.2f}】{config.fishing_coin_name}\n"
                               )
        elif 5 > len(balance_list) > 0:
            for all_rank in range(len(balance_list)):
                nickname = await get_nickname(bot, balance_list[all_rank].get("user_id"), gid)
                result += (
                    f"第{all_rank + 1}名:{nickname},{balance_list[all_rank].get('coin'):.2f}{config.fishing_coin_name}\n"
                )
            for u_rank in range(len(balance_list)):
                if balance_list[u_rank].get("user_id") == uid:
                    result += (f"您的余额排名为{u_rank + 1}:【{balance_list[u_rank].get('coin'):.2f}】{config.fishing_coin_name}\n"
                               )
        else:
            result = "暂无余额记录"
        return result


async def get_fish_caught_list(gid: str, uid: str) -> List[str]:
    result = []
    if fish_record := FishHistory.find(FishHistory.gid == gid, FishHistory.user_id == uid):
        async for fi in fish_record:
            last_fish_name = dict(fi).get("fish_name")
            result.append(last_fish_name)
    return list(set(result))


async def get_nickname(bot: Bot, user_id, group_id=None):
    """获取用户的昵称，若在群中则为群名片，不在群中为qq昵称"""
    if group_id and group_id != "global":
        info = await bot.get_group_member_info(group_id=int(group_id), user_id=int(user_id))
        other_name = info.get("card", "") or info.get("nickname", "")
        if not other_name:
            info = await bot.get_stranger_info(user_id=int(user_id))
            other_name = info.get("nickname", "")
    else:
        info = await bot.get_stranger_info(user_id=int(user_id))
        other_name = info.get("nickname", "")
    return other_name
