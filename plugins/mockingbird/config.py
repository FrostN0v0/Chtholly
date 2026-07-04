from nonebot.config import Config
from nonebot.plugin import get_plugin_config


class Config(Config):
    model: str = "nanmei"
    """模型  可选<azusa>，<nanmei>"""
    accuracy: int = 9
    """精度"""
    steps: int = 1000
    """步长"""


config = get_plugin_config(Config)
