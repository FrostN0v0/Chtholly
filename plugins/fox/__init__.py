# from kirami import on_prefix
# from kirami.typing import Matcher
# from kirami.message import Message
# from kirami.config.path import IMAGE_DIR
# from kirami.utils.resource import Image
import random
from pathlib import Path

from nonebot import on_keyword
from nonebot.adapters.onebot.v11 import MessageSegment

from utils.path import IMAGE_DIR
from utils.utils import path2base64

fox_img = on_keyword({"fox", "嘤", "狐娘表情包"}, priority=20)
FOX_DIR: Path = IMAGE_DIR / "fox_img"
img_list = [img for img in FOX_DIR.iterdir() if img.is_file()]


@fox_img.handle()
async def fox():
    img = await path2base64(random.choice(img_list))
    await fox_img.finish(MessageSegment.image(img))
