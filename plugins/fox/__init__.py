from kirami import on_prefix
from kirami.typing import Matcher
from kirami.message import Message
from kirami.config.path import IMAGE_DIR
from kirami.utils.resource import Image


@on_prefix("fox", "嘤", "狐娘表情包")
async def fox(matcher: Matcher):
    msg = Message.image(Image(IMAGE_DIR/"fox_img").choice())
    await matcher.finish(msg)
