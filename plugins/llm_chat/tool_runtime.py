"""LLM tool registration for llm_chat."""

from __future__ import annotations

import random
from typing import Protocol, cast
from pathlib import Path
from collections import deque

from launart import Launart
from arclet.entari import Audio, Image, Session, MessageChain, plugin, command, plugin_config
from entari_plugin_llm import LLMToolEvent  # entari: plugin
from arclet.entari.logger import log
from entari_plugin_database import select, get_session  # entari: plugin

from utils.path import AUDIO_DIR, IMAGE_DIR

from .tools import tts_temp_path, truncate_for_tts, is_command_allowed
from .config import LLMChatConfig
from .models import ImageTag
from .web_tools import register_web_access_tools
from .core.media import match_audio, parse_audio_text, is_random_request
from .image_tags import pick_image
from .persona.store import append_message

DINGGONG_DIR = AUDIO_DIR / "dinggong"
_RECENT_IMAGE_WINDOW = 5
_recent_images: dict[str, deque[str]] = {}
_LOGGER = log.wrapper("[llm_chat]")


class TTSServiceLike(Protocol):
    file_extension: str

    async def synthesize(self, text: str) -> bytes: ...


config = plugin_config(LLMChatConfig)
tools = plugin.dispatch(LLMToolEvent)
registered_tools: list[str] = []


@tools
async def send_image(session: Session, context: str) -> str:
    (
        """
    Send one local reaction image or sticker matching compact context keywords. """
        """Use proactively for explicit requests and natural emotional reactions in casual conversation, including """
        """greetings, teasing, embarrassment, affection, comfort, celebration, surprise, jealousy, exasperation, """
        """or light complaints. Prefer it when a sticker expresses the feeling more naturally """
        """than another text sentence. """
        """Do not wait for an explicit sticker request when the emotional fit is clear. """
        """This is not image generation, web search, or analysis of an attached image.

    Args:
        context (str): Short emotion/scenario tags, for example "害羞 可爱 早安"; "随便" picks randomly.
    Returns:
        str: Delivery result.
    """
    )
    async with get_session() as db:
        rows = list((await db.execute(select(ImageTag))).scalars().all())
    if not rows:
        return "没有可用的图片"
    recent = _recent_images.setdefault(session.channel.id, deque(maxlen=_RECENT_IMAGE_WINDOW))
    rel_path = await pick_image(config, rows, context, recent)
    if rel_path is None:
        return "没有合适的图片"
    full = IMAGE_DIR / rel_path
    if not full.exists():
        return "图片文件已丢失"
    await session.send(MessageChain([Image.of(path=full)]))
    recent.append(rel_path)
    row = next((item for item in rows if item.file_path == rel_path), None)
    tag_hint = "，".join((row.tags if row else context).split("，")[:5])
    await append_message(session.channel.id, "", "bot", "assistant", f"[发送了表情包: {tag_hint}]")
    return f"已发送图片（{context}）"


registered_tools.append("send_image")

if DINGGONG_DIR.exists():
    clip_texts = [text for file in sorted(DINGGONG_DIR.glob("*.mp3")) if (text := parse_audio_text(file.name))]
    inventory = "；".join(clip_texts)

    async def send_audio(session: Session, context: str) -> str:
        files = sorted(DINGGONG_DIR.glob("*.mp3"))
        if is_random_request(context):
            pool = [file for file in files if parse_audio_text(file.name)]
            matched = random.choice(pool) if pool else None
        else:
            matched = match_audio(context, files)
        if matched is None:
            return "没有合适的语音片段"
        await session.send(MessageChain([Audio.of(path=matched)]))
        await append_message(
            session.channel.id, "", "bot", "assistant", f"[发送了语音: {parse_audio_text(matched.name)}]"
        )
        return f"已发送语音：{parse_audio_text(matched.name)}"

    send_audio.__doc__ = (
        """
    Send one prerecorded local voice clip selected from the available clip lines. """
        """Use only when an inventory line matches; use speak for arbitrary new text. """
        f"""Do not retry with alternate wording after no match.

    Available clip lines: {inventory}

    Args:
        context (str): Tone/scenario keywords or a quote from the clip list; "随便" picks randomly.
    Returns:
        str: Spoken text in the selected clip.
    """
    )
    tools(send_audio)
    registered_tools.append("send_audio")

if config.tts_enabled:

    @tools
    async def speak(session: Session, text: str) -> str:
        (
            """
        Synthesize and send one short new utterance from the character. """
            """Use proactively when vocal delivery adds warmth, intimacy, playfulness, comfort, """
            """celebration, surprise, or a meaningful emotional turn in casual conversation. """
            """Prefer it over another plain-text sentence when tone itself carries the response. """
            """Use this for arbitrary new speech, not for selecting a prerecorded clip. """
            """Do not repeat the full spoken sentence as final text after success.

        For expressive Fish Audio speech, mix inline emotion or delivery tags directly into text at natural phrase
        boundaries. Use multiple tags when the sentence turns, for example:
        "[softly] Good evening. [happy] I am glad you are here. [whisper] Stay a little longer."
        Prefer bracket tags such as [happy], [sad], [excited], [softly], [whisper], [laughing], [sigh],
        [nervous], [calm], [emphasis], or short natural-language bracket cues.
        Do not describe the tags outside the spoken text.

        Args:
            text (str): Short text to speak, including inline emotion tags when useful.
        Returns:
            str: Delivery result.
        """
        )
        speech = truncate_for_tts(text, config.tts_max_chars)
        try:
            service = cast(TTSServiceLike, Launart.current().get_component("tts.service"))
            audio = await service.synthesize(speech)
        except Exception:
            return "语音服务暂不可用"
        out = tts_temp_path(service.file_extension)
        Path(out).write_bytes(audio)
        await session.send(MessageChain([Audio.of(path=out)]))
        await append_message(session.channel.id, "", "bot", "assistant", f"[用语音说: {speech}]")
        return f"已用语音说出：{speech}"

    registered_tools.append("speak")

if config.allowed_commands:

    @tools
    async def call_plugin(session: Session, command_line: str) -> str:
        (
            """
        Execute one whitelisted bot command only when the user explicitly asks for command execution. """
            """Remove one leading '/' or '.' from the command name before passing command_line, """
            """preserve the remaining command and necessary arguments, """
            """and do not discover, invent, broaden, or retry commands.

        Args:
            command_line (str): Full command line, for example "echo hello".
        Returns:
            str: Command result text.
        """
        )
        allowed, head = is_command_allowed(command_line, config.allowed_commands)
        if not allowed:
            return f"指令 {head or '(空)'} 不在允许列表中"
        _LOGGER.info(f"call_plugin executing whitelisted command: {head}")
        result = await command.execute(command_line, session)
        return str(result) if result is not None else "指令已执行"

    registered_tools.append("call_plugin")

registered_tools.extend(register_web_access_tools(tools, config))
