from datetime import datetime

from kirami import on_request
from kirami.event import (
    FriendRequestEvent,
    GroupRequestEvent,
)
from kirami.exception import ActionFailed
from kirami.typing import Bot

from kirami.log import logger
from kirami.utils import scheduler
from kirami.config import bot_config, DATA_DIR
from kirami.utils.jsondata import JsonDict

from .config import Config
from .utils import time_manager

config = Config.load_config()
NICKNAME = list(bot_config.nickname)[0]

friend_req = on_request(priority=5, block=True)
group_req = on_request(priority=5, block=True)

json_dict = JsonDict(path=DATA_DIR / "friends" / "friends.json", auto_load=True)
gjson_dict = JsonDict(path=DATA_DIR / "friends" / "groups.json", auto_load=True)


@friend_req.handle()
async def _(bot: Bot, event: FriendRequestEvent):
    if time_manager.add_user_request(event.user_id):
        logger.debug(f"收录好友请求...", "好友请求", target=event.user_id)
        user = await bot.get_stranger_info(user_id=event.user_id)
        nickname = user["nickname"]
        await bot.send_private_msg(
            user_id=config.master_id[0],
            message=f"*****一份好友申请*****\n"
            f"昵称：{nickname}({event.user_id})\n"
            f"自动同意：{'√' if config.auto_add_friends else '×'}\n"
            f"日期：{str(datetime.now()).split('.')[0]}\n"
            f"备注：{event.comment}",
        )
        if config.auto_add_friends:
            logger.debug(f"已开启好友请求自动同意，成功通过该请求", "好友请求", target=event.user_id)
            await bot.set_friend_add_request(flag=event.flag, approve=True)
        else:
            pass
            # 好友申请远程处理
    else:
        logger.debug(f"好友请求五分钟内重复, 已忽略", "好友请求", target=event.user_id)


@group_req.handle()
async def _(bot: Bot, event: GroupRequestEvent):
    # 邀请
    if event.sub_type == "invite":
        if str(event.user_id) in config.master_id[0]:
            try:
                logger.debug(
                    f"超级用户自动同意加入群聊", "群聊请求", event.user_id, target=event.group_id
                )
                await bot.set_group_add_request(
                    flag=event.flag, sub_type="invite", approve=True
                )
                group_info = await bot.get_group_info(group_id=event.group_id)
            except ActionFailed as e:
                logger.error(
                    "超级用户自动同意加入群聊发生错误",
                    "群聊请求",
                    event.user_id,
                    target=event.group_id,
                    e=e,
                )
        else:
            if time_manager.add_group_request(event.user_id, event.group_id):
                logger.debug(
                    f"收录 用户[{event.user_id}] 群聊[{event.group_id}] 群聊请求", "群聊请求"
                )
                user = await bot.get_stranger_info(user_id=event.user_id)
                sex = user["sex"]
                age = str(user["age"])
                nickname = event.user_id
                await bot.send_private_msg(
                    user_id=config.master_id[0],
                    message=f"*****一份入群申请*****\n"
                    f"申请人：{nickname}({event.user_id})\n"
                    f"群聊：{event.group_id}\n"
                    f"邀请日期：{datetime.now().replace(microsecond=0)}",
                )
                await bot.send_private_msg(
                    user_id=event.user_id,
                    message=f"想要邀请我偷偷入群嘛~已经提醒{NICKNAME}的管理员大人了\n"
                    "请确保已经群主或群管理沟通过！\n"
                    "等待管理员处理吧！",
                )
                # 群邀请申请远程处理
            else:
                logger.debug(
                    f"群聊请求五分钟内重复, 已忽略",
                    "群聊请求",
                    target=f"{event.user_id}:{event.group_id}",
                )


@scheduler.scheduled_job(
    "interval",
    minutes=5,
)
async def _():
    time_manager.clear()
