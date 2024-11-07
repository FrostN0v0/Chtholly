from kirami.config import BaseConfig
from kirami.config.path import IMAGE_DIR
from pydantic import validator
from pathlib import Path


class Config(BaseConfig):
    withdraw: bool = False
    """发送的图片是否在规定时间内撤回"""
    last_time: int = 90
    """图片保留时间，请设置在两分钟以内"""
    icp_id: str = "鲁ICP备2021032545号-1"
    """ICP域名备案号"""

    @validator("last_time")
    def check_last_time(cls, v):
        if isinstance(v, int) and 120 > v > 0:
            return v
        raise ValueError("消息撤回时间不合法")


config = Config.load_config()

gallery_dir = IMAGE_DIR / "gallery"

template_dir = Path(__file__).parent / "templates"

static_dir = Path(__file__).parent / "templates" / "static"
