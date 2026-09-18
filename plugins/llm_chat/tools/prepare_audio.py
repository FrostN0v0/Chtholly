"""Registered prerecorded audio preparation."""

from __future__ import annotations

from stat import S_ISREG
import random
from pathlib import Path
from dataclasses import dataclass

from arclet.entari import Audio, Session
from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from ..core.media import match_audio, parse_audio_text, is_random_request
from ..core.types import JSONType
from ._registration import register_tool
from ..core.delivery import DeliveryError
from ..prepared_media import prepare_media, ensure_media_capacity

_MAX_AUDIO_BYTES = 10 * 1024 * 1024


@dataclass
class AudioToolContext:
    """Dependencies for prerecorded audio selection and preparation."""

    audio_dir: Path


def register_prepare_audio(
    dispatcher: PluginDispatcher[JSONType],
    runtime: AudioToolContext,
) -> Subscriber[JSONType] | None:
    """Register prerecorded audio preparation when clips are available."""

    if not runtime.audio_dir.exists():
        return None

    clip_texts = [text for file in sorted(runtime.audio_dir.glob("*.mp3")) if (text := parse_audio_text(file.name))]
    inventory = "；".join(clip_texts)

    async def prepare_audio(session: Session, context: str) -> JSONType:
        files = sorted(runtime.audio_dir.glob("*.mp3"))
        if is_random_request(context):
            pool = [file for file in files if parse_audio_text(file.name)]
            matched = random.choice(pool) if pool else None
        else:
            matched = match_audio(context, files)
        if matched is None:
            return "没有合适的语音片段"
        ensure_media_capacity(1, byte_count=0)
        try:
            resolved = matched.resolve(strict=True)
            resolved.relative_to(runtime.audio_dir.resolve(strict=True))
            info = resolved.stat()
            if not S_ISREG(info.st_mode) or not 0 < info.st_size <= _MAX_AUDIO_BYTES:
                raise ValueError
            with resolved.open("rb") as source:
                data = source.read(_MAX_AUDIO_BYTES + 1)
        except (OSError, ValueError):
            raise DeliveryError("Registered audio is unavailable") from None
        if not data or len(data) > _MAX_AUDIO_BYTES:
            raise DeliveryError("Registered audio is empty or too large")
        try:
            audio = Audio.of(raw=data)
        except ValueError:
            raise DeliveryError("Registered audio format is not recognized") from None
        if not audio.src.startswith("data:audio/"):
            raise DeliveryError("Registered audio format is not recognized")
        spoken_text = parse_audio_text(matched.name)
        return prepare_media(
            session,
            audio,
            byte_count=len(data),
            tool_name="prepare_audio",
            history_marker=f"[发送了语音: {spoken_text}]",
        )

    prepare_audio.__doc__ = f"""Prepare one prerecorded voice clip without sending it.

    Use only when an inventory line matches; use synthesize_speech for arbitrary new text. Do not retry with
    alternate wording after no match. Pass the returned media_ref to send_msg.

    Available clip lines: {inventory}

    Args:
        context (str): Tone/scenario keywords or a quote from the clip list; "随便" picks randomly.
    Returns:
        dict | str: Prepared media reference or a no-match result.
    """
    return register_tool(dispatcher, prepare_audio)
