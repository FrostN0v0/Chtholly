from kirami.utils.utils import new_dir
from kirami.utils.jsondata import JsonDict
from kirami import on_prefix
from kirami.log import logger
from kirami.typing import Matcher
from kirami.depends import ArgStr, EventPlainText
from kirami.permission import SUPERUSER
from kirami.config.path import DATA_DIR, IMAGE_DIR
from kirami.hook import on_startup
from kirami.utils.utils import new_dir

create_dir = on_prefix("新建图库", permission=SUPERUSER)

json_dict = JsonDict(path=DATA_DIR / "image.json", auto_load=True)
catelog_name = ["美图", "表情包"]
catelog_key = "catelog"


@on_startup
async def check_dir():
    if not (IMAGE_DIR / "gallery").exists():
        new_dir(IMAGE_DIR / "gallery")
    else:
        for img_dir in json_dict["catelog"]:
            if not (IMAGE_DIR / "gallery" / img_dir).exists():
                new_dir(IMAGE_DIR / "gallery" / img_dir)

@create_dir.handle()
async def add_dir(get_msg: EventPlainText, matcher: Matcher):
    if get_msg:
        if catelog_key not in json_dict:
            json_dict[catelog_key] = catelog_name  # 如果列表不存在，创建一个默认列表
        for key_name in json_dict[catelog_key]:
            if key_name == get_msg:
                await matcher.finish("该目录已存在")
            else:
                pass
        json_dict[catelog_key].append(get_msg)
        new_dir(IMAGE_DIR/"gallery"/get_msg)
        json_dict.save()
        logger.success(f"新增 {get_msg} 公开图库目录")
        await matcher.finish("新建成功！")


@create_dir.got("cate", prompt="请输入要新建的目录")
async def got_dir(cate: ArgStr, matcher: Matcher):
    if catelog_key not in json_dict:
        json_dict[catelog_key] = catelog_name  # 如果列表不存在，创建一个默认列表
    for key_name in json_dict[catelog_key]:
        if key_name == cate:
            await matcher.finish("该目录已存在")
        else:
            pass
    json_dict[catelog_key].append(cate)
    new_dir(IMAGE_DIR / "gallery" / cate)
    json_dict.save()
    logger.success(f"新增 {cate} 公开图库目录")
    await matcher.finish("新建成功！")
