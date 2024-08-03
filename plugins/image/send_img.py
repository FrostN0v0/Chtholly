from kirami import on_message
from kirami.typing import Bot
from kirami.utils.jsondata import JsonDict
from kirami.message import MessageSegment
from kirami.utils.resource import Resource
from kirami.log import logger
from kirami.config.path import DATA_DIR, IMAGE_DIR
from kirami.event import MessageEvent, GroupMessageEvent
from kirami.utils.helpers import extract_plain_text
from pathlib import Path
import asyncio
import os
from .config import Config
from utils.utils import path2base64

config = Config.load_config()

json_dict = JsonDict(path=DATA_DIR / "image.json", auto_load=True)


def rule(event: MessageEvent) -> bool:
    """
    检测文本是否是关闭功能命令
    :param event: pass
    """
    msg = extract_plain_text(event.message)
    for x in json_dict["catelog"]:
        if msg.startswith(x):
            return True
    return False


send_img = on_message(rule=rule)


@send_img.handle()
async def _(event: MessageEvent, bot: Bot):
    result = Path()
    msg = extract_plain_text(event.message).split()
    gallery = msg[0]
    if gallery not in json_dict["catalog"]:
        return
    img_id = None
    if len(msg) > 1:
        img_id = msg[1]
    path = IMAGE_DIR / 'gallery' / gallery
    if gallery in json_dict["catalog"]:
        if not path.exists() and (path.parent.parent / gallery).exists():
            path = IMAGE_DIR / gallery
        else:
            path.mkdir(parents=True, exist_ok=True)
    length = len(os.listdir(path))
    if length == 0:
        logger.warning(f'图库 {gallery} 为空，调用取消！')
        await send_img.finish("该图库中没有图片噢")
    try:
        if msg[1]:
            result = path / f"{msg[1]}.png"
    except IndexError:
        result = Resource.image(path).choice()
    pic = Resource.image(result).name
    if result:
        msg_id = await send_img.send(f"id：{pic.split('.')[0]}" + MessageSegment.image(await path2base64(result.path)))
        if config.withdraw:
            await asyncio.sleep(config.last_time)
            await bot.delete_msg(message_id=msg_id["message_id"])
    else:
        logger.info(
            f"(USER {event.user_id}, GROUP "
            f"{event.group_id if isinstance(event, GroupMessageEvent) else 'private'}) "
            f"发送 {gallery} 失败"
        )
        await send_img.finish(f"不想给你看Ov|")
