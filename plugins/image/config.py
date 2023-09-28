from kirami.config import BaseConfig
from pydantic import validator


class Config(BaseConfig):
    withdraw: bool = False
    """发送的图片是否在规定时间内撤回"""
    last_time: int = 90
    """图片保留时间，请设置在两分钟以内"""

    @validator("last_time")
    def check_last_time(self, v):
        if isinstance(v, int) and 120 > v > 0:
            return v
        raise ValueError("消息撤回时间不合法")
