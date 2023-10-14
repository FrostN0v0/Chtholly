from kirami.utils.resource import Resource
from kirami.config.path import AUDIO_DIR
from kirami import on_keyword
from nonebot.adapters.red import Bot
from nonebot.adapters.red.event import MessageEvent
from nonebot.adapters.red.message import MessageSegment

dg_voice = on_keyword("骂", to_me=True)


@dg_voice.handle()
async def dg(bot: Bot, event: MessageEvent):
    audio = Resource.audio(AUDIO_DIR/"dinggong").choice()
    audio_path = Resource.audio(audio).path
    await bot.send(event, MessageSegment.voice(audio_path))
