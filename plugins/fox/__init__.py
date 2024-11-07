from kirami import on_prefix
from kirami.typing import Matcher
from kirami.message import Message
from kirami.config.path import IMAGE_DIR
from kirami.utils.resource import Image
from utils.utils import path2base64


@on_prefix("fox", "嘤", "狐娘表情包", priority=20)
async def fox(matcher: Matcher):
    img = Image(IMAGE_DIR/"fox_img").choice()
    msg = Message.image(await path2base64(Image(img).path))
    await matcher.finish(msg)
