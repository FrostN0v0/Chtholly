from kirami.config import BaseConfig
from pydantic import validator


class Config(BaseConfig):
    model: str = "nanmei"
    """模型  可选<azusa>，<nanmei>"""
    accuracy: int = 9
    """精度"""
    steps: int = 1000
    """步长"""

    @validator("accuracy")
    def check_accuracy(cls, v):
        if isinstance(v, int) and 10 > v > 2:
            return v
        raise ValueError("精度不合法")

    @validator("steps")
    def check_steps(cls, v):
        if isinstance(v, int) and 2001 > v > 199:
            return v
        raise ValueError("步长不合法")

    @validator("model")
    def check_model(cls, v):
        if isinstance(v, str) and v in ["azusa", "nanmei"]:
            return v
        raise ValueError("模型不合法或未定义")