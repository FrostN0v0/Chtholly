from typing import List
from kirami.config import BaseConfig, DATA_DIR
from pathlib import Path


class Config(BaseConfig):

    fish_dir: Path = DATA_DIR / "fishing"

    fishing_limit: int = 1800

    fish_multiplier: float = 1.0

    fishing_time_radius: List = [30, 150]

    fishing_coin_name: str = "块小鱼干"  # It means Fishing Coin.

    fish_empty_chance: float = 0.1


config = Config.load_config()
