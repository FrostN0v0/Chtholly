from kirami import on_command
from nonebot.adapters.red.message import MessageSegment
from kirami.utils.request import Request
from kirami.utils.utils import get_api_data

cat = on_command("随机猫猫", "来个猫猫")
fox = on_command("随机狐狸", "来个狐狸")
husky = on_command("随机二哈", "来个二哈")


@cat.handle()
async def random_cat():
    response = await Request.get("http://edgecats.net/")
    await cat.send(message=MessageSegment.image(response.content))


@fox.handle()
async def random_fox():
    response = await get_api_data("https://randomfox.ca/floof/")
    await fox.send(message=MessageSegment.image(response["image"]))


@husky.handle()
async def random_dog():
    response = await get_api_data("https://dog.ceo/api/breed/husky/images/random")
    await husky.send(message=MessageSegment.image(response["message"]))
