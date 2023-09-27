from kirami import on_prefix
from kirami.matcher import Matcher
from kirami.message import MessageSegment
from kirami.utils.request import Request
from kirami.utils.utils import get_api_data

cat = on_prefix("随机猫猫", "来个猫猫")
fox = on_prefix("随机狐狸", "来个狐狸")
husky = on_prefix("随机二哈", "来个二哈")


@cat.handle()
async def random_cat(matcher: Matcher):
    response = await Request.get("http://edgecats.net/")
    await matcher.send(message=MessageSegment.image(response.content))


@fox.handle()
async def random_fox(matcher: Matcher):
    response = await get_api_data("https://randomfox.ca/floof/")
    await matcher.send(message=MessageSegment.image(response["image"]))


@husky.handle()
async def random_dog(matcher: Matcher):
    response = await get_api_data("https://dog.ceo/api/breed/husky/images/random")
    await matcher.send(message=MessageSegment.image(response["message"]))
