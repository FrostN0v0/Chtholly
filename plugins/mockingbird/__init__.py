import asyncio

import langid
from pydub import AudioSegment
from io import BytesIO
from nonebot.log import logger
from nonebot.rule import to_me
from nonebot import get_driver, on_command
from nonebot.typing import T_State as State
from nonebot.params import ArgStr, CommandArg
from nonebot.adapters.onebot.v11.message import Message, MessageSegment

from utils.path import RES_DIR, AUDIO_DIR
from utils.mockingbirdforuse import MockingBird

from .config import config
from .data_source import get_ai_voice
from .res_download import check_dir, check_resource, download_resource

mockingbird = MockingBird()

mockingbird_path = RES_DIR / "mockingbird"

voice = on_command("说", rule=to_me(), priority=5)

driver = get_driver()


@driver.on_startup
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
        mockingbird.set_synthesizer(
            mockingbird_path / config.model / f"{config.model}.pt"
        )
        logger.success(f"已加载模型 {config.model} ")
        return True
    except Exception as e:
        return f"{type(e)}：{e}"


@voice.handle()
async def _(state: State, arg: Message = CommandArg()):
    args = arg.extract_plain_text().strip()
    if args:
        state["words"] = args


@voice.got("words", prompt="想要让Bot说什么话呢?")
async def _(words: str = ArgStr()):
    words = words.strip().replace("\n", "").replace("\r", "")
    if langid.classify(words)[0] == "ja":
        record = await get_ai_voice(words)
        if record is None:
            await voice.finish("语音合成失败，请稍后再试。")
        else:
            with open(AUDIO_DIR / 'mocking.wav', 'wb') as file:
                file.write(record.getvalue())
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
        with open(AUDIO_DIR / 'mocking.mp3', 'wb') as file:
            file.write(record.getvalue())
        record_bytes = record.getvalue()
        audio = AudioSegment.from_file(BytesIO(record_bytes))
        audio.export(AUDIO_DIR/"mocking.mp3", format="mp3")
    if record is None:
        await voice.finish("语音合成失败，请稍后再试。")
    else:
        await voice.finish(MessageSegment.record(AUDIO_DIR/"mocking.mp3"))
