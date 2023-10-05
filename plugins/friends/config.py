from kirami.config import BaseConfig
from kirami.config import bot_config


class Config(BaseConfig):
    auto_add_friends: bool = True
    """自动同意添加好友"""
    master_id: list[str] = list(bot_config.superusers)
