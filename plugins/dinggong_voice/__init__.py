import random
from pathlib import Path

from nonebot import on_keyword
from nonebot.rule import to_me
from nonebot.matcher import Matcher
from nonebot.adapters.onebot.v11 import MessageSegment

from utils.path import AUDIO_DIR

DINGGONG_DIR: Path = AUDIO_DIR / "dinggong"
dg_voice = on_keyword({"骂"}, rule=to_me())
audio_list = [audio for audio in DINGGONG_DIR.iterdir() if audio.is_file()]


@dg_voice.handle()
async def dg(matcher: Matcher):
    msg = random.choice(audio_list)
    await matcher.finish(MessageSegment.record(msg))
