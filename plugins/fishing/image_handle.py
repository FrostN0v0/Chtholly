import base64
import os
from PIL import Image, ImageFont, ImageDraw
from PIL.Image import Resampling
from kirami.log import logger
from kirami.config import RES_DIR
from .data_source import get_fish_caught_list

from io import BytesIO


PRELOAD = True  # 是否启动时直接将所有图片加载到内存中以提高查看仓库的速度(增加约几M内存消耗)
COL_NUM = 11    # 查看仓库时每行显示的卡片个数
__BASE = os.path.split(os.path.realpath(__file__))

STAMP_PATH = RES_DIR / "image" / "fish"

font = ImageFont.truetype(str(RES_DIR / "font" / 'Uranus_Pixel_11Px.ttf'), 16)
card_file_names_all = []
image_cache = {}

image_list = os.listdir(STAMP_PATH)
for image in image_list:
    # 图像缓存
    if PRELOAD:
        image_path = os.path.join(STAMP_PATH, image)
        img = Image.open(image_path)
        image_cache[image] = img.convert('RGBA') if img.mode != 'RGBA' else img
    card_file_names_all.append(image)
len_card = len(card_file_names_all)
print(card_file_names_all)
logger.info(f"共加载{len_card}个鱼类图片")


def get_pic(pic_path, grey):
    try:
        if PRELOAD:
            sign_image = image_cache[pic_path]
        else:
            sign_image = Image.open(os.path.join(str(__BASE), 'stamp', pic_path))
    except KeyError as e:
        raise KeyError(f"Image not found: {pic_path}") from e
    sign_image = sign_image.resize((48, 48), Resampling.LANCZOS)
    if grey:
        sign_image = sign_image.convert('L')
    return sign_image


async def handbook_card_image(gid: str, uid: str):
    row_num = len_card // 11 if len_card % 11 != 0 else len_card // 11 - 1
    base = Image.open(RES_DIR / "image" / 'frame.png')
    base = base.resize((40 + 11 * 80 + (11 - 1) * 10, 150 + row_num * 80 + (row_num - 1) * 10),
                       Resampling.LANCZOS)
    row_index_offset = 0
    row_offset = 0
    cards_list = card_file_names_all
    caught_list = await get_fish_caught_list(gid, uid)
    draw = ImageDraw.Draw(base)
    for index, c_id in enumerate(cards_list):
        row_index = index // 11 + row_index_offset
        col_index = index % 11
        f = get_pic(c_id, False) if c_id[:-4] in caught_list else get_pic(c_id, True)
        base.paste(f, (
            30 + col_index * 80 + (col_index - 1) * 10, row_offset + 40 + row_index * 80 + (row_index - 1) * 10), f)
        text_name = c_id.split('.')[0] if c_id[:-4] in caught_list else '???'
        text_bbox = draw.textbbox((0, 0), text_name, font=font)  # 获取文本边界框
        text_width = text_bbox[2] - text_bbox[0]
        text_x = 30 + col_index * 80 + (col_index - 1) * 10 + (48 - text_width) // 2  # 居中对齐
        text_y = row_offset + 40 + row_index * 80 + (row_index - 1) * 10 + 48 + 5  # 图片下方留5像素间隔
        draw.text((text_x, text_y), text_name, font=font, fill="black")
    row_offset += 30
    buf = BytesIO()
    base = base.convert('RGB')
    base.save(buf, format='JPEG')
    base64_str = f'base64://{base64.b64encode(buf.getvalue()).decode()}'
    return base64_str
