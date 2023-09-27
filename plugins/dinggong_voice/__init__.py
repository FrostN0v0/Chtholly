from kirami.utils.resource import Resource
from kirami.config.path import AUDIO_DIR
from kirami import on_keyword
from kirami.matcher import Matcher
from kirami.message import MessageSegment

dg_voice = on_keyword("骂", to_me=True)


@dg_voice.handle()
async def dg(matcher: Matcher):
    msg = Resource.audio(AUDIO_DIR/"dinggong").choice()
    await matcher.finish(MessageSegment.record(msg))
