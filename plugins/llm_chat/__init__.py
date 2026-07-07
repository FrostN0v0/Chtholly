"""Interactive group chat plugin.

Galgame-style multi-axis relationship engine (LLM-evaluated), vision image
tagging, context-aware media sending, optional TTS voice replies and plugin
function calling. Hard deps: llm + database plugins. TTS is optional.
"""

import base64
import asyncio
from datetime import datetime

from launart import Launart
from arclet.entari import (
    Audio,
    Image,
    Session,
    MessageChain,
    MessageCreatedEvent,
    plugin,
    command,
    filter_,
    metadata,
    scheduler,
    plugin_config,
)
from arclet.letoderea import BLOCK
from entari_plugin_llm import LLMToolEvent, llm  # entari: plugin
from arclet.entari.logger import log
from arclet.entari.plugin import PluginRole
from entari_plugin_database import select, get_session  # entari: plugin
from arclet.letoderea.context import Contexts

from utils.path import AUDIO_DIR, IMAGE_DIR

from .media import match_audio, match_image, parse_audio_text
from .tools import tts_temp_path, truncate_for_tts
from .config import LLMChatConfig
from .models import ImageTag
from .persona.store import (
    get_mood,
    set_mood,
    get_relation,
    load_history,
    nightly_decay,
    save_relation,
    append_message,
)
from .persona.runner import run_evaluation
from .persona.compose import energy_at, compose_persona_prompt

metadata(
    name="llm_chat",
    author=[{"name": "FrostN0v0"}],
    version="0.1.0",
    description="群聊会话互动：多轴关系引擎 + 媒体互动 + 可选语音回复",
    role=PluginRole.NORMAL,
)

config = plugin_config(LLMChatConfig)
plug = plugin.Plugin.current()

HELP_META = {"icon": "\U0001f4ac", "category": "互动"}

_LOGGER = log.wrapper("[llm_chat]")
DINGGONG_DIR = AUDIO_DIR / "dinggong"
registered_tools: list[str] = []


def _sentence_truncate(text: str, limit: int) -> str:
    return truncate_for_tts(text, limit)


def _setup_tools() -> list[str]:
    tools = plugin.dispatch(LLMToolEvent)
    registered: list[str] = []

    @tools
    async def send_image(session: Session, context: str) -> str:
        """
        Send a local image that matches the requested context.

        Args:
            context (str): A few keywords describing the desired image.
        Returns:
            str: Delivery result.
        """
        async with get_session() as db:
            rows = (await db.execute(select(ImageTag))).scalars().all()
        rel_path = match_image(context, [(row.file_path, row.tags) for row in rows])
        if rel_path is None:
            return "没有合适的图片"
        full = IMAGE_DIR / rel_path
        if not full.exists():
            return "图片文件已丢失"
        await session.send(MessageChain([Image.of(path=full)]))
        return f"已发送图片（{context}）"

    registered.append("send_image")

    if DINGGONG_DIR.exists():

        @tools
        async def send_audio(session: Session, context: str) -> str:
            """
            Send a prerecorded local voice clip matching the context.

            Args:
                context (str): The intended tone or short phrase.
            Returns:
                str: Spoken text in the selected clip.
            """
            matched = match_audio(context, sorted(DINGGONG_DIR.glob("*.mp3")))
            if matched is None:
                return "没有合适的语音片段"
            await session.send(MessageChain([Audio.of(path=matched)]))
            return f"已发送语音：{parse_audio_text(matched.name)}"

        registered.append("send_audio")

    if config.tts_enabled:

        @tools
        async def speak(session: Session, text: str) -> str:
            """
            Synthesize a short sentence and send it as voice.

            Args:
                text (str): Short text to speak.
            Returns:
                str: Delivery result.
            """
            speech = _sentence_truncate(text, config.tts_max_chars)
            try:
                service = Launart.current().get_component("tts.service")
                audio = await service.synthesize(speech)  # type: ignore[attr-defined]
            except Exception:
                return "语音服务暂不可用"
            out = tts_temp_path()
            from pathlib import Path

            Path(out).write_bytes(audio)
            await session.send(MessageChain([Audio.of(path=out)]))
            return f"已用语音说出：{speech}"

        registered.append("speak")

    if config.allowed_commands:

        @tools
        async def call_plugin(command_line: str) -> str:
            """
            Execute one whitelisted bot command.

            Args:
                command_line (str): Full command line, for example "echo hello".
            Returns:
                str: Command result text.
            """
            head = command_line.split(maxsplit=1)[0] if command_line.strip() else ""
            if head not in config.allowed_commands:
                return f"指令 {head or '(空)'} 不在允许列表中"
            result = await command.execute(command_line)
            return str(result) if result is not None else "指令已执行"

        registered.append("call_plugin")

    registered_tools[:] = registered
    return registered


_registered = _setup_tools()
_LOGGER.info(f"registered LLM tools: {', '.join(_registered) or '(none)'}")


@plug.use("::startup")
async def _tag_images_on_startup():
    if not config.image_tags_enabled:
        return
    task = asyncio.create_task(_tag_images())
    plugin.collect_disposes(task.cancel)


async def _tag_images():
    """Tag untagged local images with vision keywords, rate-limited per startup."""
    try:
        async with get_session() as db:
            known = {row.file_path for row in (await db.execute(select(ImageTag))).scalars().all()}
        candidates = [
            p
            for p in sorted(IMAGE_DIR.rglob("*"))
            if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp") and str(p.relative_to(IMAGE_DIR)) not in known
        ]
        batch = candidates[: config.tag_batch_size]
        if not batch:
            return
        _LOGGER.info(f"tagging {len(batch)} images ({len(candidates)} untagged remain)")
        for path in batch:
            try:
                data = base64.b64encode(path.read_bytes()).decode()
                mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
                resp = await llm.vision(
                    f"data:{mime};base64,{data}",
                    system="用5个中文关键词描述这张图，逗号分隔，只输出关键词。",
                )
                tags = (resp.choices[0].message.content or "").strip()  # type: ignore[union-attr]
                if not tags:
                    continue
                async with get_session() as db:
                    db.add(ImageTag(file_path=str(path.relative_to(IMAGE_DIR)), tags=tags))
                    await db.commit()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                _LOGGER.warning(f"tagging failed for {path.name}: {e!r}")
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass


@scheduler.cron("0 4 * * *")
async def _nightly_decay():
    await nightly_decay()
    _LOGGER.info("nightly relationship decay applied")


@plug.dispatch(MessageCreatedEvent).register(priority=900)
@filter_.to_me
async def on_chat(session: Session, ctx: Contexts):
    content = session.elements.extract_plain_text().strip()
    if not content:
        return None

    channel_id = session.channel.id
    user_id = session.user.id
    user_name = (session.member.nick if session.member else None) or session.user.name or user_id

    rel = await get_relation(user_id, channel_id)
    mood = await get_mood(channel_id)
    energy = energy_at(datetime.now().hour)

    history = await load_history(channel_id, config.context_window)
    messages = [
        {
            "role": row.role if row.role == "assistant" else "user",
            "content": f"[{row.user_name}]: {row.content}" if row.role == "user" else row.content,
        }
        for row in history
    ]
    messages.append({"role": "user", "content": f"[{user_name}]: {content}"})

    system = compose_persona_prompt(
        config.persona,
        mood,
        energy,
        affection=rel.affection,
        trust=rel.trust,
        dependence=rel.dependence,
        resentment=rel.resentment,
        familiarity=rel.familiarity,
        impression=rel.impression,
        user_name=user_name,
    )

    try:
        response = await llm.generate(messages, system=system, model=config.model, ctx=ctx)
    except Exception as e:
        _LOGGER.warning(f"llm generate failed: {e!r}")
        return None

    reply = response.choices[0].message.content or ""  # type: ignore[union-attr]
    if reply and reply != "[END_OF_RESPONSE]":
        await session.send(reply)

    await append_message(channel_id, user_id, user_name, "user", content)
    if reply and reply != "[END_OF_RESPONSE]":
        await append_message(channel_id, "", "bot", "assistant", reply)

    # Mechanical familiarity bump; all other axes move only via the evaluator.
    familiarity = min(100.0, rel.familiarity + 1)
    axes = {
        "affection": rel.affection,
        "trust": rel.trust,
        "dependence": rel.dependence,
        "resentment": rel.resentment,
    }
    impression = rel.impression
    counter = rel.eval_counter + 1

    if counter >= config.eval_every_n:
        counter = 0
        transcript = [f"[{user_name}]: {content}"]
        if reply and reply != "[END_OF_RESPONSE]":
            transcript.append(f"[你]: {reply}")
        try:
            result = await run_evaluation(config, config.persona, axes, impression, transcript)
        except Exception as e:
            _LOGGER.warning(f"relationship evaluation failed: {e!r}")
            result = None
        if result is not None:
            from .persona.eval import apply_deltas

            axes = apply_deltas(axes, result)
            impression = result.impression
            await set_mood(channel_id, mood + result.mood_delta)

    await save_relation(
        user_id,
        channel_id,
        axes=axes,
        impression=impression,
        familiarity=familiarity,
        eval_counter=counter,
    )
    return BLOCK
