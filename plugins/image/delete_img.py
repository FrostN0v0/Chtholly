from kirami import on_prefix
from kirami.typing import Matcher
from kirami.utils.jsondata import JsonDict
from kirami.permission import SUPERUSER
from kirami.message import MessageSegment
from kirami.log import logger
from kirami.config.path import DATA_DIR, IMAGE_DIR
from kirami.event import MessageEvent, GroupMessageEvent
from kirami.state import State
from kirami.depends import ArgStr, EventPlainText
from kirami.hook import on_startup
from kirami.utils.utils import new_dir
import os

delete_img = on_prefix("删除图片", to_me=True, permission=SUPERUSER)

json_dict = JsonDict(path=DATA_DIR / "image.json", auto_load=True)


@on_startup
async def check_dir():
    if not (DATA_DIR / "trash_bin").exists():
        new_dir(DATA_DIR / "trash_bin")


@delete_img.handle()
async def _(state: State, arg: EventPlainText):
    args = arg.strip().split()
    if args:
        if args[0] in json_dict["catelog"]:
            state["path"] = args[0]
        if len(args) > 0:
            state["img_id"] = args[1]


@delete_img.got("path", prompt="请输入要删除的目标图库？")
@delete_img.got("img_id", prompt="请输入要删除的图片id？")
async def delete(path: ArgStr, img_id: ArgStr, event: MessageEvent, state: State, matcher: Matcher):
    if path in ["取消", "算了"] or img_id in ["取消", "算了"]:
        await delete_img.finish("已取消操作...")
    if path not in json_dict["catelog"]:
        await delete_img.reject_arg("path", "此目录不正确，请重新输入目录！")
    if not img_id.isdigit():
        await delete_img.reject_arg("id", "id不正确！请重新输入数字...")
    path = IMAGE_DIR / 'gallery' / path
    if not path.exists() and (path.parent.parent / state['path']).exists():
        path = path.parent.parent / state['path']
    max_id = len(os.listdir(path))
    if int(img_id) > max_id or int(img_id) < 0:
        await delete_img.finish(f"Id超过上下限，上限：{max_id}", at_sender=True)
    try:
        if (DATA_DIR / "trash_bin" / f"{event.user_id}_delete.png").exists():
            (DATA_DIR / "trash_bin" / f"{event.user_id}_delete.png").unlink()
        logger.info(f"删除{state['path']}图片 {img_id}.png 成功")
    except Exception as e:
        logger.warning(f"删除图片 {img_id}.png 失败 e{e}")
    try:
        os.rename(path / f"{img_id}.png", DATA_DIR / "trash_bin" / f"{event.user_id}_delete.png")
        logger.info(f"移动 {path}/{img_id}.png 移动成功")
    except Exception as e:
        logger.warning(f"{path}/{img_id}.png --> 移动失败 e:{e}")
    if not os.path.exists(path / f"{img_id}.png"):
        try:
            if int(img_id) != max_id:
                os.rename(path / f"{max_id}.png", path / f"{img_id}.png")
        except FileExistsError as e:
            logger.error(f"{path}/{max_id}.png 替换 {path}/{img_id}.png 失败 e:{e}")
        logger.info(f"{path}/{max_id}.png 替换 {path}/{img_id}.png 成功")
        logger.info(
            f"USER {event.user_id} GROUP {event.group_id if isinstance(event, GroupMessageEvent) else 'private'}"
            f" -> id: {img_id} 删除成功"
        )
        await matcher.finish(
            f"id: {img_id} 删除成功" + MessageSegment.image(DATA_DIR / "trash_bin" / f"{event.user_id}_delete.png", ),
            at_sender=True
        )
    await matcher.finish(f"id: {img_id} 删除失败！")
