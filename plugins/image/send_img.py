from kirami import on_message
from kirami.typing import Bot
from kirami.message import MessageSegment
from kirami.utils.resource import Resource
from kirami.log import logger
from kirami.config.path import IMAGE_DIR
from kirami.event import MessageEvent, GroupMessageEvent
from kirami.utils.helpers import extract_plain_text
from pathlib import Path

import random
import asyncio
import os
from .config import Config
from utils.utils import path2base64

from .new_Catalog import get_gallery_list

config = Config.load_config()


def rule(event: MessageEvent) -> bool:
    """
    检测文本是否是关闭功能命令
    :param event: pass
    """
    msg = extract_plain_text(event.message)
    for x in get_gallery_list():
        if msg.startswith(x):
            return True
    return False


send_img = on_message(rule=rule)


@send_img.handle()
async def _(event: MessageEvent, bot: Bot):
    result = Path()
    msg = extract_plain_text(event.message).split()
    gallery_name = msg[0]
    if gallery_name not in get_gallery_list():
        await send_img.finish(f"图库[{gallery_name}]不存在")
    img_path = IMAGE_DIR / 'gallery' / gallery_name
    length = len(os.listdir(img_path))
    if length == 0:
        logger.warning(f'图库 {gallery_name} 为空，调用取消！')
        await send_img.finish("该图库中没有图片噢")
    try:
        if msg[1]:
            result = img_path / f"{msg[1]}.png"
    except IndexError:
        result = Resource.image(img_path).choice().path
    pic = Resource.image(result).name
    if result and random.random() < 0.9:
        msg_id = await send_img.send(f"id：{pic.split('.')[0]}" + MessageSegment.image(await path2base64(result)))
        if config.withdraw:
            await asyncio.sleep(config.last_time)
            await bot.delete_msg(message_id=msg_id["message_id"])
    else:
        logger.info(
            f"(USER {event.user_id}, GROUP "
            f"{event.group_id if isinstance(event, GroupMessageEvent) else 'private'}) "
            f"发送 {gallery_name} 失败"
        )
        await send_img.finish(f"不想给你看Ov|")
