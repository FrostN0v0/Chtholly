from kirami import on_command
from kirami.config.path import RES_DIR
from kirami.message import MessageSegment
from kirami.state import State
from kirami.depends import CommandArg, ArgStr
from kirami.utils.helpers import extract_plain_text
from kirami.hook import on_startup
from kirami.log import logger
from utils.mockingbirdforuse import MockingBird
from .config import Config
from .data_source import get_ai_voice
from .res_download import download_resource, check_resource, check_dir

import asyncio
import langid

config = Config.load_config()

mockingbird = MockingBird()

mockingbird_path = RES_DIR / "mockingbird"

voice = on_command("说", to_me=True)


@on_startup
async def init_mockingbird():
    try:
        await check_dir(mockingbird_path, config.model)
        if not await check_resource(mockingbird_path, config.model):
            if await download_resource(mockingbird_path, config.model):
                logger.success("模型下载成功...")
            else:
                logger.error("模型下载失败，请检查网络...")
                return False
        logger.info("开始加载 MockingBird 模型...")
        mockingbird.load_model(
            mockingbird_path / "encoder.pt",
            mockingbird_path / "g_hifigan.pt",
            # Path(os.path.join(mockingbird_path, "wavernn.pt"))
        )
        mockingbird.set_synthesizer(mockingbird_path / config.model / f"{config.model}.pt")
        logger.success(f"已加载模型 {config.model} ")
        return True
    except Exception as e:
        return f"{type(e)}：{e}"


@voice.handle()
async def _(state: State, arg: CommandArg):
    args = extract_plain_text(arg).strip()
    if args:
        state["words"] = args


@voice.got("words", prompt=f"想要让Bot说什么话呢?")
async def _(words: ArgStr):
    words = words.strip().replace('\n', '').replace('\r', '')
    if langid.classify(words)[0] == "ja":
        record = await get_ai_voice(words)
    else:
        record = await asyncio.get_event_loop().run_in_executor(
            None,
            mockingbird.synthesize,
            str(words),
            mockingbird_path / config.model / "record.wav",
            "HifiGan",
            0,
            config.accuracy,
            config.steps,
        )
    await voice.finish(MessageSegment.record(record))
