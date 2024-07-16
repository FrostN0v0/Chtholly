import time
from mango import Document, Field
from datetime import datetime


class FishHistory(Document):
    gid: str
    user_id: str
    fish_name: str
    weight: float
    fish_stamp: int = int(time.time())


class FishUserData(Document):
    gid: str
    user_id: str
    coin: float = Field(default=0, ge=0)
    register_time: datetime = datetime.now()
