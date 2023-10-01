from kirami import get_driver
from kirami.log import logger
from pathlib import Path
from kirami.config import BaseConfig
from kirami.config.path import DATA_DIR
from typing import Dict, Union
try:
    import ujson as json
except ModuleNotFoundError:
    import json
from .utils import morning_json_update


class Config(BaseConfig):
    morning_path: Path = DATA_DIR / "morning"


driver = get_driver()

morning_config = Config.load_config()


@driver.on_startup
async def _() -> None:
    if not morning_config.morning_path.exists():
        morning_config.morning_path.mkdir(parents=True, exist_ok=True)

    config_json_path: Path = morning_config.morning_path / "config.json"

    # Initial default config, global for all groups
    _config: Dict[str, Dict[str, Dict[str, Union[bool, int]]]] = {
        "morning": {
            "morning_intime": {
                "enable": True,
                "early_time": 6,
                "late_time": 12
            },
            "multi_get_up": {
                "enable": False,
                "interval": 6
            },
            "super_get_up": {
                "enable": False,
                "interval": 3
            }
        },
        "night": {
            "night_intime": {
                "enable": True,
                "early_time": 21,
                "late_time": 6
            },
            "good_sleep": {
                "enable": True,
                "interval": 6
            },
            "deep_sleep": {
                "enable": False,
                "interval": 3
            }
        }
    }

    if not config_json_path.exists():
        with open(config_json_path, 'w', encoding='utf-8') as f:
            json.dump(_config, f, ensure_ascii=False, indent=4)

        logger.info("Initialized the config.json of Morning plugin")

    # Old data.json will be transferred from v0.2.x into v0.3.x version automatically
    new_data_path: Path = morning_config.morning_path / "morning.json"

    if not new_data_path.exists():
        with open(new_data_path, 'w', encoding='utf-8') as f:
            json.dump(dict(), f, ensure_ascii=False, indent=4)

        logger.info("已创建数据文件！")
