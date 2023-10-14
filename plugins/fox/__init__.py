from kirami import on_prefix
from kirami.typing import Matcher
from nonebot.adapters.red.message import MessageSegment
from kirami.config.path import IMAGE_DIR
from kirami.utils.resource import Resource


@on_prefix("fox", "嘤", "狐娘表情包", priority=20)
async def fox(matcher: Matcher):
    msg = Resource.image(IMAGE_DIR/"fox_img").choice()
    img_path = Resource.image(msg).path
    await matcher.finish(MessageSegment.image(img_path))
