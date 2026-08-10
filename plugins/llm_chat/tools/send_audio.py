"""send_audio LLM tool implementation."""

from __future__ import annotations

import random
from pathlib import Path
from dataclasses import dataclass
from collections.abc import Callable, Awaitable

from arclet.entari import Audio, Session, MessageChain
from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from ._delivery import send_with_delivery
from ..core.media import match_audio, parse_audio_text, is_random_request
from ..core.types import JSONType
from ._registration import register_tool
from ..core.delivery import reserve_media_message, current_llm_chat_delivery

HistoryAppender = Callable[[str, str, str, str, str], Awaitable[object]]


@dataclass
class AudioToolContext:
    """Mutable dependencies for prerecorded audio selection and delivery."""

    audio_dir: Path
    append_history: HistoryAppender


def register_send_audio(
    dispatcher: PluginDispatcher[JSONType],
    runtime: AudioToolContext,
) -> Subscriber[JSONType] | None:
    """Register prerecorded audio delivery when clips are available."""

    if not runtime.audio_dir.exists():
        return None

    clip_texts = [text for file in sorted(runtime.audio_dir.glob("*.mp3")) if (text := parse_audio_text(file.name))]
    inventory = "；".join(clip_texts)

    async def send_audio(session: Session, context: str) -> str:
        files = sorted(runtime.audio_dir.glob("*.mp3"))
        if is_random_request(context):
            pool = [file for file in files if parse_audio_text(file.name)]
            matched = random.choice(pool) if pool else None
        else:
            matched = match_audio(context, files)
        if matched is None:
            return "没有合适的语音片段"
        delivery_state = current_llm_chat_delivery()
        if delivery_state is not None:
            delivery_state = reserve_media_message()
        await send_with_delivery(
            session,
            MessageChain([Audio.of(path=matched)]),
            delivery_state,
            media=True,
        )
        spoken_text = parse_audio_text(matched.name)
        await runtime.append_history(
            session.channel.id,
            "",
            "bot",
            "assistant",
            f"[发送了语音: {spoken_text}]",
        )
        return f"已发送语音：{spoken_text}"

    send_audio.__doc__ = f"""Send one prerecorded local voice clip selected from the available clip lines.

    Use only when an inventory line matches; use speak for arbitrary new text. Do not retry with alternate wording
    after no match.

    Available clip lines: {inventory}

    Args:
        context (str): Tone/scenario keywords or a quote from the clip list; "随便" picks randomly.
    Returns:
        str: Spoken text in the selected clip.
    """
    return register_tool(dispatcher, send_audio)
