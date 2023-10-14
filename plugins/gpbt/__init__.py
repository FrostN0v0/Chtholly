from kirami import on_command
from nonebot.adapters.red.event import MessageEvent
from nonebot.adapters.red.message import MessageSegment
from kirami.depends import ArgStr, CommandArg
from .dataget import generator


gpbt = on_command("狗屁不通", "狗屁不通文章生成器", "gpbt", to_me=True)


@gpbt.handle()
async def handle_gpbt(get_msg: CommandArg, event: MessageEvent):
    if get_msg:
        msg_list = str(get_msg).split(" ")
        topic = msg_list[0]
        word = int(msg_list[1])
        msg = generator(topic, word)
        await gpbt.finish(MessageSegment.text(msg))


@gpbt.got('topic', prompt="请输入要生成的文章主题关键词")
@gpbt.got('word', prompt="请输入要生成的文章字数（建议1k字以下，不要超过消息发送上限）")
async def got_gpbt(topic: ArgStr, word: ArgStr):
    msg = generator(topic, int(word))
    await gpbt.finish(MessageSegment.text(msg))
