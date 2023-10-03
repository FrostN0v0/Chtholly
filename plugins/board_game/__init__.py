import shlex

from asyncio import TimerHandle
from dataclasses import dataclass
from typing import Dict, List, Optional, NoReturn

from kirami.matcher import Matcher
from kirami.rule import ArgumentParser
from kirami.exception import ParserExit
from kirami import on_command
from kirami.depends import CommandArg, Bot
from kirami.event import MessageEvent, GroupMessageEvent
from kirami.message import (
    Message,
    MessageSegment,
)

from .go import Go
from .gomoku import Gomoku
from .othello import Othello
from .game import Game, MoveResult, Player, Pos

parser = ArgumentParser("boardgame", description="棋类游戏")
parser.add_argument("-r", "--rule", help="棋局规则")
group = parser.add_mutually_exclusive_group()
group.add_argument("-e", "--stop", "--end", action="store_true", help="停止下棋")
group.add_argument("-v", "--show", "--view", action="store_true", help="显示棋盘")
group.add_argument("--skip", action="store_true", help="跳过回合")
group.add_argument("--repent", action="store_true", help="悔棋")
group.add_argument("--reload", action="store_true", help="重新加载已停止的游戏")
parser.add_argument("--white", action="store_true", help="后手")
parser.add_argument("position", nargs="?", help="落子位置")


def get_cid(bot: Bot, event: MessageEvent):
    if isinstance(event, MessageEvent):
        cid = f"{bot.self_id}_{event.sub_type}_"

    if isinstance(event, GroupMessageEvent):
        cid += str(event.group_id)

    return cid


def game_running(bot: Bot, event: GroupMessageEvent) -> bool:
    cid = get_cid(bot, event)
    return bool(games.get(cid, None))


wuzi = on_command("五子棋", to_me=True)
luozi = on_command("落子", rule=game_running)
weiqi = on_command("围棋", to_me=True)
heibai = on_command("黑白棋", "奥赛罗", to_me=True)
gg = on_command("停止下棋", "结束下棋", "停止游戏", "结束游戏", rule=game_running)
show_table = on_command("查看棋盘", "查看棋局", "显示棋盘", "显示棋局", rule=game_running)
sk = on_command("跳过回合", rule=game_running)
repent = on_command("悔棋", rule=game_running)
load_wuzi = on_command("重载五子棋棋局", "恢复五子棋棋局")
load_othello = on_command("重载黑白棋棋局", "恢复黑白棋棋局")
load_go = on_command("重载围棋棋局", "恢复围棋棋局")


@wuzi.handle()
async def wuzi(matcher: Matcher, bot: Bot, event: GroupMessageEvent, msg: CommandArg):
    args = ["--rule", "gomoku"]
    await handle_boardgame(matcher, bot, event, args)


@weiqi.handle()
async def weiqi(matcher: Matcher, bot: Bot, event: GroupMessageEvent):
    args = ["--rule", "go"]
    await handle_boardgame(matcher, bot, event, args)


@heibai.handle()
async def heibai(matcher: Matcher, bot: Bot, event: GroupMessageEvent):
    args = ["--rule", "othello"]
    await handle_boardgame(matcher, bot, event, args)


@gg.handle()
async def gg(matcher: Matcher, bot: Bot, event: GroupMessageEvent):
    args = ["--stop"]
    await handle_boardgame(matcher, bot, event, args)


@show_table.handle()
async def show_table(matcher: Matcher, bot: Bot, event: GroupMessageEvent):
    args = ["--show"]
    await handle_boardgame(matcher, bot, event, args)


@sk.handle()
async def sk(matcher: Matcher, bot: Bot, event: GroupMessageEvent):
    args = ["--skip"]
    await handle_boardgame(matcher, bot, event, args)


@repent.handle()
async def repent(matcher: Matcher, bot: Bot, event: GroupMessageEvent):
    args = ["--repent"]
    await handle_boardgame(matcher, bot, event, args)

@load_wuzi.handle()
async def load_wuzi(matcher: Matcher, bot: Bot, event: GroupMessageEvent):
    args = ["--rule", "gomoku", "--reload"]
    await handle_boardgame(matcher, bot, event, args)


@load_go.handle()
async def load_wuzi(matcher: Matcher, bot: Bot, event: GroupMessageEvent):
    args = ["--rule", "go", "--reload"]
    await handle_boardgame(matcher, bot, event, args)


@load_othello.handle()
async def load_wuzi(matcher: Matcher, bot: Bot, event: GroupMessageEvent):
    args = ["--rule", "othello", "--reload"]
    await handle_boardgame(matcher, bot, event, args)


@luozi.handle()
async def wuzi(matcher: Matcher, bot: Bot, event: GroupMessageEvent, msg: CommandArg):
    try:
        args = shlex.split(msg.extract_plain_text().strip())
    except:
        args = []
    await handle_boardgame(matcher, bot, event, args)


def new_player(event: GroupMessageEvent) -> Player:
    return Player(str(event.user_id), event.sender.card or event.sender.nickname or "")


@dataclass
class Options:
    rule: str = ""
    stop: bool = False
    show: bool = False
    skip: bool = False
    repent: bool = False
    reload: bool = False
    white: bool = False
    position: str = ""


games: Dict[str, Game] = {}
timers: Dict[str, TimerHandle] = {}


async def stop_game(matcher: Matcher, cid: str):
    timers.pop(cid, None)
    if games.get(cid, None):
        game = games.pop(cid)
        await matcher.finish(f"{game.name}下棋超时，游戏结束，可发送“重载{game.name}棋局”继续下棋")


async def handle_boardgame(matcher: Matcher, bot: Bot, event: GroupMessageEvent, argv: List[str]):
    async def send(
            message: Optional[str] = None, image: Optional[bytes] = None
    ) -> NoReturn:
        if not (message or image):
            await matcher.finish()
        msg = Message()
        if message:
            msg.append(message)
        if image:
            msg.append(MessageSegment.image(image))
        await matcher.finish(msg)

    try:
        args = parser.parse_args(argv)
    except ParserExit as e:
        if e.status == 0:
            await send("@我 + “五子棋”、“黑白棋”、“围棋”开始一局游戏;\n"
                       "再发送“落子 字母+数字”下棋，如“落子 A1”;\n"
                       "发送“结束下棋”结束当前棋局；发送“显示棋盘”显示当前棋局")
        await send()

    options = Options(**vars(args))

    cid = get_cid(bot, event)
    if not games.get(cid, None):
        if options.stop or options.show or options.repent or options.skip:
            await send("没有正在进行的游戏")

        if not options.rule:
            await send("@我 + “五子棋”、“黑白棋”、“围棋”开始一局游戏。")

        rule = options.rule
        if rule in ["go", "围棋"]:
            Game = Go
        elif rule in ["gomoku", "五子棋"]:
            Game = Gomoku
        elif rule in ["othello", "黑白棋"]:
            Game = Othello
        else:
            await send("没有找到对应的规则，目前支持：围棋(go)、五子棋(gomoku)、黑白棋(othello)")

        if options.reload:
            game = await Game.load_record(cid)
            if not game:
                await matcher.finish("没有找到被中断的游戏")
            games[cid] = game
            await send(
                f"游戏发起时间：{game.start_time.strftime('%Y-%m-%d %H:%M:%S')}\n黑方：{game.player_black}\n白方：{game.player_white}\n下一手轮到：{game.player_next}",
                await game.draw(),
            )

        game = Game()
        player = new_player(event)
        if options.white:
            game.player_white = player
        else:
            game.player_black = player

        games[cid] = game
        await game.save_record(cid)
        await send(
            f"{player} 发起了游戏 {game.name}！\n发送“落子 字母+数字”下棋，如“落子 A1”", await game.draw()
        )

    game = games[cid]
    # set_timeout(matcher, cid)
    player = new_player(event)

    if options.stop:
        if (not game.player_white or game.player_white != player) and (
                not game.player_black or game.player_black != player
        ):
            await send("只有游戏参与者才能结束游戏")
        game = games.pop(cid)
        await send(f"游戏已结束，可发送“重载{game.name}棋局”继续下棋")

    if options.show:
        await send(image=await game.draw())

    if options.rule:
        await send("当前有正在进行的游戏，可发送“停止下棋”结束游戏")

    if (
            game.player_black
            and game.player_white
            and game.player_black != player
            and game.player_white != player
    ):
        await send("游戏已经开始，无法加入")

    if options.skip:
        if not game.allow_skip:
            await send("当前游戏不允许跳过回合")
        if game.player_next and game.player_next != player:
            await send("当前不是你的回合")
        game.update(Pos.null())
        await game.save_record(cid)
        msg = f"{player} 选择跳过其回合"
        if game.player_next:
            msg += f"，下一手轮到 {game.player_next}"
        await send(msg)

    if options.repent:
        if len(game.history) <= 1:
            await matcher.finish("对局尚未开始")
        if game.player_last and game.player_last != player:
            await send("上一手棋不是你所下")
        game.pop()
        await game.save_record(cid)
        await send(f"{player} 进行了悔棋", await game.draw())

    if (game.player_next and game.player_next != player) or (
            game.player_last and game.player_last == player
    ):
        await send("当前不是你的回合")

    position = options.position
    if not position:
        await send("发送“落子 字母+数字”下棋，如“落子 A1”")

    try:
        pos = Pos.from_str(position)
    except ValueError:
        await send("请发送正确的坐标")

    if not game.in_range(pos):
        await send("落子超出边界")

    if game.get(pos):
        await send("此处已有落子")

    try:
        result = game.update(pos)
    except ValueError as e:
        await send(f"非法落子：{e}")

    if game.player_last:
        msg = f"{player} 落子于 {position.upper()}"
    else:
        if not game.player_black:
            game.player_black = player
        elif not game.player_white:
            game.player_white = player
        msg = f"{player} 加入了游戏并落子于 {position.upper()}"

    if result == MoveResult.ILLEGAL:
        await send("非法落子")
    elif result == MoveResult.SKIP:
        msg += f"，下一手依然轮到 {player}"
    elif result:
        games.pop(cid)
        game.is_game_over = True
        if result == MoveResult.BLACK_WIN:
            msg += f"，恭喜 {game.player_black} 获胜！"
        elif result == MoveResult.WHITE_WIN:
            msg += f"，恭喜 {game.player_white} 获胜！"
        elif result == MoveResult.DRAW:
            msg += f"，本局游戏平局"
    else:
        if game.player_next:
            msg += f"，下一手轮到 {game.player_next}"
    await game.save_record(cid)
    await send(msg, await game.draw())
