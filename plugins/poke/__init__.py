import random
from pathlib import Path

from nonebot import on_notice
from nonebot.matcher import Matcher
from nonebot.adapters.onebot.v11 import MessageSegment, PokeNotifyEvent

from utils.path import AUDIO_DIR, IMAGE_DIR

poke__reply = [
    "lsp你再戳？",
    "连个可爱美少女都要戳的肥宅真恶心啊。",
    "你再戳！",
    "？再戳试试？",
    "别戳了别戳了再戳就坏了555",
    "我爪巴爪巴，球球别再戳了",
    "你戳你🐎呢？！",
    "那...那里...那里不能戳...绝对...",
    "(。´・ω・)ん?",
    "有事恁叫我，别天天一个劲戳戳戳！",
    "欸很烦欸！你戳🔨呢",
    "?",
    "再戳一下试试？",
    "???",
    "正在关闭对您的所有服务...关闭成功",
    "啊呜，太舒服刚刚竟然睡着了。什么事？",
    "正在定位您的真实地址...定位成功。轰炸机已起飞",
]

DINGGONG_DIR: Path = AUDIO_DIR / "dinggong"
FOX_DIR: Path = IMAGE_DIR / "fox_img"

poke = on_notice()

img_list = [img for img in FOX_DIR.iterdir() if img.is_file()]
audio_list = [audio for audio in DINGGONG_DIR.iterdir() if audio.is_file()]


@poke.handle()
async def poke_event(event: PokeNotifyEvent, matcher: Matcher):
    if event.self_id == event.target_id:
        if random.random() < 0.3:
            rst = ""
            if random.random() < 0.15:
                rst = "气死我了！"
            await matcher.finish(rst + random.choice(poke__reply), at_sender=True)
        rand = random.random()
        if rand <= 0.3:
            msg = MessageSegment.image(random.choice(img_list))
            await matcher.finish(msg)
        elif 0.3 < rand < 0.6:
            msg = random.choice(audio_list)
            await matcher.finish(MessageSegment.record(msg))
        else:
            await matcher.send(MessageSegment("poke", {"qq": event.user_id}))
