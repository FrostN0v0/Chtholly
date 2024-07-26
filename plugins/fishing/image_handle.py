import base64
import os
import json

from PIL import Image, ImageFont, ImageDraw, UnidentifiedImageError
from PIL.Image import Resampling
from kirami.log import logger
from kirami.config import IMAGE_DIR, DATA_DIR, FONT_DIR
from kirami.hook import on_startup
from kirami.utils.downloader import Downloader
from kirami.utils.request import Request

from .data_source import get_fish_caught_list

import zipfile
from io import BytesIO


PRELOAD = True  # 是否启动时直接将所有图片加载到内存中以提高查看仓库的速度(增加约几M内存消耗)
COL_NUM = 11    # 查看仓库时每行显示的卡片个数

Fish_Path = IMAGE_DIR / 'fish'
DATA_Path = DATA_DIR / 'fishing'

card_file_names_all = []
image_cache = {}
len_card = 0


@on_startup
async def init_fishing():
    global len_card
    if not DATA_Path.exists():
        DATA_Path.mkdir(parents=True)
    if not Fish_Path.exists():
        Fish_Path.mkdir()
        with zipfile.ZipFile(DATA_Path / 'fish.zip') as zip_ref:
            for member in zip_ref.infolist():
                try:
                    decoded_name = member.filename.encode('cp437').decode('gbk')
                except:
                    decoded_name = member.filename
                member.filename = decoded_name
                zip_ref.extract(member, Fish_Path)
                logger.info(f"成功解压文件{member.filename}")
    if not (FONT_DIR / "Uranus_Pixel_11Px.ttf").exists():
        await Downloader.download_file(
            url='https://raw.githubusercontent.com/FrostN0v0/kirami-plugin-fishing/master'
                '/resources/Uranus_Pixel_11Px.ttf',
            path=FONT_DIR
        )
        logger.info("下载字体成功")
    if not (DATA_Path / 'fishes.json').exists():
        fishdata = await Request.get(
            url="https://raw.githubusercontent.com/FrostN0v0/kirami-plugin-fishing/master/resources/fishes.json")
        if fishdata.status_code == 200:
            with open(DATA_Path / 'fishes.json', "w", encoding="utf-8") as file:
                json.dump(fishdata.json(), file, ensure_ascii=False, indent=4)
            logger.info("获取数据成功")
        else:
            logger.error("获取数据失败，请检查网络是否正常")

    image_list = os.listdir(Fish_Path)
    for image in image_list:
        # 图像缓存
        if PRELOAD:
            image_path = os.path.join(Fish_Path, image)
            img = Image.open(image_path)
            image_cache[image] = img.convert('RGBA') if img.mode != 'RGBA' else img
        card_file_names_all.append(image)
    len_card = len(card_file_names_all)
    logger.info(f"共加载{len_card}个鱼类图片")


def get_pic(pic_path, grey):
    try:
        if PRELOAD:
            sign_image = image_cache[pic_path]
        else:
            sign_image = Image.open(Fish_Path / pic_path)
    except (KeyError, FileNotFoundError, UnidentifiedImageError):
        return None
    sign_image = sign_image.resize((48, 48), Resampling.LANCZOS)
    if grey:
        sign_image = sign_image.convert('L')
    return sign_image


async def handbook_card_image(gid: str, uid: str):
    row_num = len_card // 11 if len_card % 11 != 0 else len_card // 11 - 1
    base = Image.open(IMAGE_DIR / 'frame.png')
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
        text_bbox = draw.textbbox((0, 0), text_name,
                                  font=ImageFont.truetype(str(FONT_DIR / 'Uranus_Pixel_11Px.ttf'), 16))  # 获取文本边界框
        text_width = text_bbox[2] - text_bbox[0]
        text_x = 30 + col_index * 80 + (col_index - 1) * 10 + (48 - text_width) // 2  # 居中对齐
        text_y = row_offset + 40 + row_index * 80 + (row_index - 1) * 10 + 48 + 5  # 图片下方留5像素间隔
        draw.text((text_x, text_y), text_name,
                  font=ImageFont.truetype(str(FONT_DIR / 'Uranus_Pixel_11Px.ttf'), 16), fill="black")
    row_offset += 30
    buf = BytesIO()
    base = base.convert('RGB')
    base.save(buf, format='JPEG')
    base64_str = f'base64://{base64.b64encode(buf.getvalue()).decode()}'
    return base64_str
