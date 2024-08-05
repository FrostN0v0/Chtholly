from kirami import on_prefix
from kirami.log import logger
from kirami.typing import Matcher
from kirami.depends import ArgStr, EventPlainText
from kirami.permission import SUPERUSER
from kirami.config.path import IMAGE_DIR
from kirami.utils.utils import new_dir

create_dir = on_prefix("新建图库", permission=SUPERUSER)

gallery_path = IMAGE_DIR / "gallery"


if not gallery_path.exists():
    new_dir(gallery_path)


@create_dir.handle()
async def add_dir(get_msg: EventPlainText, matcher: Matcher):
    if get_msg:
        if get_msg in get_gallery_list():
            await matcher.finish("该目录已存在")
        else:
            pass
        new_dir(IMAGE_DIR/"gallery"/get_msg)
        logger.success(f"新增 {get_msg} 公开图库目录")
        await matcher.finish(f"新建图库{get_msg}成功！")


@create_dir.got("catalog", prompt="请输入要新建的图库目录名称")
async def got_dir(catalog: ArgStr, matcher: Matcher):
    if catalog in get_gallery_list():
        await matcher.finish("该目录已存在")
    else:
        pass
    new_dir(IMAGE_DIR / "gallery" / catalog)
    logger.success(f"新增 {catalog} 公开图库目录")
    await matcher.finish(f"新建图库{catalog}成功！")


def get_gallery_list():
    gallery_list = []
    for g_path in gallery_path.iterdir():
        gallery_list.append(g_path.name)
    return gallery_list
