from typing import List
from kirami.config import BaseConfig, DATA_DIR


class Config(BaseConfig):

    # 钓鱼CD时间
    fishing_limit: int = 1800
    # 获取积分倍率
    fish_multiplier: float = 1.0
    # 上钩时间[最小时间,最大时间]
    fishing_time_radius: List = [30, 150]
    # 毫无意义的配置项
    fishing_coin_name: str = "块小鱼干"
    # 空军概率
    fish_empty_chance: float = 0.1


config = Config.load_config()
