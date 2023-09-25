from kirami import on_prefix
from kirami.typing import Matcher
from kirami.depends import ArgStr, EventPlainText
from .dataget import generator


gpbt = on_prefix("狗屁不通", "狗屁不通文章生成器", "gpbt", to_me=True)


@gpbt.handle()
async def handle_gpbt(get_msg: EventPlainText, matcher: Matcher):
    if get_msg:
        msg_list = get_msg.split(" ")
        topic = msg_list[0]
        word = int(msg_list[1])
        msg = generator(topic, word)
        await matcher.finish(msg)


@gpbt.got('topic', prompt="请输入要生成的文章主题关键词")
@gpbt.got('word', prompt="请输入要生成的文章字数（建议1k字以下，不要超过消息发送上限）")
async def got_gpbt(topic: ArgStr, word: ArgStr, matcher: Matcher):
    msg = generator(topic, int(word))
    await matcher.finish(msg)
