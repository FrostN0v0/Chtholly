"""Interactive group chat plugin.

Galgame-style multi-axis relationship engine (LLM-evaluated), vision image
tagging, context-aware media sending, optional TTS voice replies and plugin
function calling. Hard deps: llm + database plugins. TTS is optional.
"""

import base64
import asyncio
from datetime import datetime

from launart import Launart
import litellm
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
from arclet.entari.filter import superusers
from arclet.entari.logger import log
from arclet.entari.plugin import PluginRole
from entari_plugin_database import select, get_session  # entari: plugin
from arclet.letoderea.context import Contexts
from entari_plugin_llm.config import get_model_config

from utils.path import AUDIO_DIR, IMAGE_DIR

from .media import match_audio, match_image, parse_audio_text, normalize_image_tags
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
from .persona.memory_store import load_memory_context, apply_memory_updates

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
        Send a local reaction image or sticker matching compact keywords.

        Args:
            context (str): Short emotion/scenario tags, for example "害羞 可爱 早安"; not a full sentence.
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
            Send a prerecorded local voice clip matching compact keywords.

            Args:
                context (str): Short tone/scenario/line keywords, for example "早安 问好"; not a full sentence.
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
    task = asyncio.create_task(_tag_images(config.tag_batch_size, retag=False))
    plugin.collect_disposes(task.cancel)


async def _generate_image_tags(data_url: str) -> str:
    """Generate normalized image tags without LiteLLM's stale vision capability gate."""
    model = get_model_config(config.image_tag_model)
    response = await litellm.acompletion(
        model=model.name,
        messages=[
            {
                "role": "system",
                "content": config.image_tag_prompt,
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Tag this image for chat reaction retrieval."},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ],
        base_url=model.base_url,
        api_key=model.api_key,
        **model.extra,
    )
    return normalize_image_tags((response.choices[0].message.content or "").strip())  # type: ignore[union-attr]


async def _tag_images(limit: int | None = None, *, retag: bool = False) -> tuple[int, int, int]:
    """Tag local images with vision keywords and return tagged, failed, remaining."""
    tagged = 0
    failed = 0
    remaining = 0
    try:
        async with get_session() as db:
            known = {row.file_path for row in (await db.execute(select(ImageTag))).scalars().all()}
        candidates = [
            p
            for p in sorted(IMAGE_DIR.rglob("*"))
            if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")
            and (retag or str(p.relative_to(IMAGE_DIR)) not in known)
        ]
        batch = candidates if limit is None else candidates[: max(0, limit)]
        remaining = max(0, len(candidates) - len(batch))
        if not batch:
            return tagged, failed, remaining
        scope = "retagging" if retag else "tagging"
        _LOGGER.info(f"{scope} {len(batch)} images ({remaining} remain)")
        for path in batch:
            rel_path = str(path.relative_to(IMAGE_DIR))
            try:
                data = base64.b64encode(path.read_bytes()).decode()
                mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
                tags = await _generate_image_tags(f"data:{mime};base64,{data}")
                if not tags:
                    failed += 1
                    continue
                async with get_session() as db:
                    existing = (
                        await db.execute(select(ImageTag).where(ImageTag.file_path == rel_path))
                    ).scalar_one_or_none()
                    if existing is None:
                        db.add(ImageTag(file_path=rel_path, tags=tags))
                    else:
                        existing.tags = tags
                    await db.commit()
                tagged += 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                failed += 1
                _LOGGER.warning(f"tagging failed for {path.name}: {e!r}")
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    return tagged, failed, remaining


@command.on("llmchat retag-images")
@superusers()
async def retag_images(session: Session):
    """Retag up to 50 local chat images with the configured vision model."""
    tagged, failed, remaining = await _tag_images(50, retag=True)
    await session.send(f"retag images done: tagged={tagged}, failed={failed}, remaining={remaining}")


@command.on("llmchat retag-images-all")
@superusers()
async def retag_images_all(session: Session):
    """Retag all local chat images with the configured vision model."""
    tagged, failed, remaining = await _tag_images(None, retag=True)
    await session.send(f"retag images done: tagged={tagged}, failed={failed}, remaining={remaining}")


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

    memory_context = await load_memory_context(config, user_id, channel_id, content)

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
        profile_facts=memory_context.profile_facts,
        relevant_memories=memory_context.relevant_memories,
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
        recent = history[-config.eval_context_window :] if config.eval_context_window > 0 else []

        def _transcript_line(row) -> str:
            if row.role == "assistant":
                return f"[你]: {row.content}"
            if row.user_id == user_id:
                return f"[评估对象 {row.user_name}]: {row.content}"
            return f"[{row.user_name}]: {row.content}"

        transcript = [_transcript_line(row) for row in recent]
        transcript.append(f"[评估对象 {user_name}]: {content}")
        if reply and reply != "[END_OF_RESPONSE]":
            transcript.append(f"[你]: {reply}")
        try:
            result = await run_evaluation(
                config,
                config.persona,
                axes,
                impression,
                memory_context.profile_facts,
                transcript,
                user_name,
            )
        except Exception as e:
            _LOGGER.warning(f"relationship evaluation failed: {e!r}")
            result = None
        if result is not None:
            from .persona.eval import apply_deltas

            axes = apply_deltas(axes, result)
            impression = result.impression
            try:
                await apply_memory_updates(config, user_id, channel_id, result)
            except Exception as e:
                _LOGGER.warning(f"memory update failed: {e!r}")
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
