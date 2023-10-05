import os
from PIL import Image, ImageFont
from kirami.event import GroupMessageEvent
from kirami.config import DATA_DIR, IMAGE_DIR, FONT_DIR
from kirami.log import logger
from kirami import Bot

try:
    import ujson as json
except:
    import json

PRELOAD = True  # 是否启动时直接将所有图片加载到内存中以提高查看仓库的速度(增加约几M内存消耗)
COL_NUM = 11    # 查看仓库时每行显示的卡片个数
__BASE = os.path.split(os.path.realpath(__file__))

FRAME_DIR_PATH = IMAGE_DIR
GOODWILL_PATH = DATA_DIR / "sign"
STAMP_PATH = IMAGE_DIR / "stamp"

font = ImageFont.truetype(str(FONT_DIR / 'arial.ttf'), 16)
card_file_names_all = []

image_cache = {}
if not os.path.exists(GOODWILL_PATH / "goodwill.json"):
    # 不存在就创建文件
    userdata = {}
    with open(GOODWILL_PATH / "goodwill.json", "w", encoding="utf-8") as f:
        json.dump(userdata, f, indent=4, ensure_ascii=False)
    # 资源预检
image_list = os.listdir(STAMP_PATH)
for image in image_list:
    # 图像缓存
    if PRELOAD:
        image_path = os.path.join(STAMP_PATH, image)
        img = Image.open(image_path)
        image_cache[image] = img.convert('RGBA') if img.mode != 'RGBA' else img
    card_file_names_all.append(image)
len_card = len(card_file_names_all)
logger.info(f"共加载{len_card}个签到印章图片")


def normalize_digit_format(n):
    return f'0{n}' if n < 10 else f'{n}'


def get_pic(pic_path, grey):
    if PRELOAD:
        sign_image = image_cache[pic_path]
    else:
        sign_image = Image.open(os.path.join(__BASE, 'stamp', pic_path))
    sign_image = sign_image.resize((80, 80), Image.ANTIALIAS)
    if grey:
        sign_image = sign_image.convert('L')
    return sign_image


async def get_at(event: GroupMessageEvent) -> int:
    """获取被艾特用户 ID"""
    msg = event.get_message()
    for msg_seg in msg:
        if msg_seg.type == "at":
            return -1 if msg_seg.data["qq"] == "all" else int(msg_seg.data["qq"])
    return -1


async def get_user_card(bot: Bot, group_id, qid):
    # 返还用户nickname
    user_info: dict = await bot.get_group_member_info(group_id=group_id, user_id=qid)
    user_card = user_info["card"]
    if not user_card:
        user_card = user_info["nickname"]
    return user_card
