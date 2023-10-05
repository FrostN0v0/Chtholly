from typing import Optional
from kirami.config import BaseConfig

from .model import HourlyType


class Config(BaseConfig):
    qweather_apikey: Optional[str] = None
    qweather_apitype: Optional[str] = None
    qweather_hourlytype: Optional[HourlyType] = HourlyType.current_12h
    debug: bool = False


plugin_config = Config.load_config()

QWEATHER_APIKEY = plugin_config.qweather_apikey
QWEATHER_APITYPE = plugin_config.qweather_apitype
QWEATHER_HOURLYTYPE = plugin_config.qweather_hourlytype
DEBUG = plugin_config.debug
