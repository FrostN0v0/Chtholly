from kirami.utils.jsondata import JsonDict
from kirami import on_prefix
from kirami.log import logger
from kirami.typing import Matcher
from kirami.event import MessageEvent, GroupMessageEvent
from kirami.state import State
from kirami.depends import Arg, ArgStr
from kirami.utils.downloader import Downloader
from kirami.config.path import DATA_DIR, IMAGE_DIR
from kirami.utils.helpers import extract_image_urls, extract_plain_text
import os

upload_img = on_prefix("上传图片", to_me=True)
show_gallery = on_prefix("查看公开图库", to_me=True)

json_dict = JsonDict(path=DATA_DIR / "image.json", auto_load=True)


@upload_img.handle()
async def upload(event: MessageEvent, state: State):
    args = extract_plain_text(event.message).strip()
    img_list = extract_image_urls(event.message)
    if args:
        if args in json_dict["catelog"]:
            state.path = args
    if img_list:
        state.img_list = event.message.get("image")


@upload_img.got(
    "path",
    prompt=f"请选择要上传的图库\n- "
    + "\n- ".join(json_dict["catelog"]),
)
@upload_img.got("img_list", prompt="图呢图呢图呢图呢！GKD！")
async def _(path: ArgStr, img_list: Arg, matcher: Matcher, event: MessageEvent):
    if path not in json_dict["catelog"]:
        await matcher.reject_arg("path", "此目录不正确，请重新输入目录！")
    if not extract_image_urls(img_list):
        print(img_list)
        await matcher.reject_arg("img_list", "图呢图呢图呢图呢！GKD！")
    img_list = extract_image_urls(img_list)
    group_id = 0
    user_id = event.user_id
    if isinstance(event, GroupMessageEvent):
        group_id = event.group_id
    img_id = len(os.listdir(IMAGE_DIR / 'gallery' / path)) + 1
    failed_list = []
    success_id = ""
    for img in img_list:
        if await Downloader.download_file(img, IMAGE_DIR/'gallery'/path, file_name=str(img_id), file_type="png"):
            success_id += str(img_id) + "，"
            img_id += 1
        else:
            failed_list.append(img)
    failed_result = ""
    for img_fail in failed_list:
        failed_result += str(img_fail) + "\n"
    logger.info(
        f"USER {user_id}  GROUP {group_id}"
        f" 上传图片至 {path} 共 {len(img_list)} 张，失败 {len(failed_list)} 张，id={success_id[:-1]}"
    )
    if failed_list:
        await matcher.send(
            f"这次一共为 {path}库 添加了 {len(img_list) - len(failed_list)} 张图片\n"
            f"依次的Id为：{success_id[:-1]}\n上传失败：{failed_result[:-1]}\n感谢您对图库的扩充!WW"
        )
    else:
        await matcher.send(
            f"这次一共为 {path}库 添加了 {len(img_list)} 张图片\n依次的Id为："
            f"{success_id[:-1]}\n感谢您对图库的扩充!WW"
        )


@show_gallery.handle()
async def show(matcher: Matcher):
    x = "公开图库列表：\n"
    for i, gallery in enumerate(json_dict["catelog"], 1):
        x += f"\t{i}.{gallery}\n"
    await matcher.send(x)
