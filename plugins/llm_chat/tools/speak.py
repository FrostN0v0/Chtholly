"""speak LLM tool implementation."""

from __future__ import annotations

from typing import Protocol
from dataclasses import dataclass
from collections.abc import Callable, Awaitable

from arclet.entari import Audio, Session, MessageChain
from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from ..config import LLMChatConfig
from .support import truncate_for_tts
from ._delivery import send_with_delivery
from ..core.types import JSONType
from ._registration import register_tool
from ..core.delivery import reserve_media_message, current_llm_chat_delivery

HistoryAppender = Callable[[str, str, str, str, str], Awaitable[object]]
AudioFactory = Callable[[bytes, str], Audio]


class TTSServiceLike(Protocol):
    file_extension: str

    async def synthesize(self, text: str) -> bytes: ...


ServiceFactory = Callable[[], TTSServiceLike]


@dataclass
class SpeakToolContext:
    """Mutable dependencies for synthesized voice delivery."""

    config: LLMChatConfig
    get_service: ServiceFactory
    make_audio: AudioFactory
    append_history: HistoryAppender


def register_speak(
    dispatcher: PluginDispatcher[JSONType],
    runtime: SpeakToolContext,
) -> Subscriber[JSONType] | None:
    """Register synthesized speech when TTS is enabled."""

    if not runtime.config.tts_enabled:
        return None

    async def speak(session: Session, text: str) -> str:
        """Synthesize and send one short new utterance from the character.

        Use proactively when vocal delivery adds warmth, intimacy, playfulness, comfort, celebration, surprise, or a
        meaningful emotional turn in casual conversation.
        Prefer it over another plain-text sentence when tone itself carries the response.
        Use this for arbitrary new speech, not for selecting a prerecorded clip.
        Do not repeat the full spoken sentence as final text after success.

        For expressive Fish Audio speech, mix inline emotion or delivery tags directly into text at natural phrase
        boundaries. Use multiple tags when the sentence turns, for example:
        "[softly] Good evening. [happy] I am glad you are here. [whisper] Stay a little longer."
        Prefer bracket tags such as [happy], [sad], [excited], [softly], [whisper], [laughing], [sigh], [nervous],
        [calm], [emphasis], or short natural-language bracket cues. Do not describe the tags outside the spoken text.

        Args:
            text (str): Short text to speak, including inline emotion tags when useful.
        Returns:
            str: Delivery result.
        """

        speech = truncate_for_tts(text, runtime.config.tts_max_chars)
        delivery_state = current_llm_chat_delivery()
        if delivery_state is not None:
            delivery_state = reserve_media_message()
        try:
            service = runtime.get_service()
            audio = await service.synthesize(speech)
        except Exception:
            return "语音服务暂不可用"
        await send_with_delivery(
            session,
            MessageChain([runtime.make_audio(audio, service.file_extension)]),
            delivery_state,
            media=True,
        )
        await runtime.append_history(session.channel.id, "", "bot", "assistant", f"[用语音说: {speech}]")
        return f"已用语音说出：{speech}"

    return register_tool(dispatcher, speak)
