from kirami import on_prefix
from kirami.typing import Matcher
from kirami.message import Message
from kirami.config.path import IMAGE_DIR


@on_prefix("帮助", "菜单", "help")
async def menu(matcher: Matcher):
    msg = Message.image(f'{IMAGE_DIR}/help.png')
    await matcher.finish(msg)
