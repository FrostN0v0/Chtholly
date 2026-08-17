"""speak LLM tool implementation."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable, Awaitable

from arclet.entari import Audio, Session, MessageChain
from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from utils.tts_service_core.providers import TTSSynthesisError

from ._tts import ServiceFactory
from ..config import LLMChatConfig
from .support import truncate_for_tts
from ._delivery import send_with_delivery
from ..core.types import JSONType
from ._registration import register_tool
from ..core.delivery import reserve_media_message, current_llm_chat_delivery

HistoryAppender = Callable[[str, str, str, str, str], Awaitable[object]]
AudioFactory = Callable[[bytes, str], Audio]



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

    async def speak(
        session: Session,
        text: str,
        version: str = "",
        model_name: str = "",
        reference_language: str = "",
        emotion: str = "",
        text_language: str = "",
        speed: float | None = None,
    ) -> str:
        """Synthesize and send one short new utterance with an optional live voice selection.

        Use proactively when vocal delivery adds warmth, intimacy, playfulness, comfort, celebration, surprise,
        or a meaningful emotional turn in casual conversation.
        Prefer it over another plain-text sentence when tone itself carries the response.
        Use this for arbitrary new speech, not for selecting a prerecorded clip.
        Do not repeat the full spoken sentence as final text after success.

        For GPT-SoVITS, call list_tts_voices before choosing a character, version, reference language, or emotion. Pass
        exact catalog values. If the user explicitly requests a character, never substitute another character when the
        requested model is absent. Omit selectors to use the configured provider default. GPT-SoVITS emotion is a
        separate argument and bracketed Fish Audio style tags must not be embedded in its text.

        For Fish Audio, leave GPT-SoVITS selectors empty. Only use bracketed emotion or delivery tags when
        list_tts_voices reports supports_inline_style_tags=true.

        Args:
            text (str): Short text to speak.
            version (str): Exact GPT-SoVITS version returned by list_tts_voices, or empty for provider default.
            model_name (str): Exact GPT-SoVITS character model, or empty for provider default.
            reference_language (str): Exact reference language for the selected model.
            emotion (str): Exact reference emotion for the selected model and reference language.
            text_language (str): Exact synthesis language label returned by list_tts_voices.
            speed (float | None): Optional speech speed within the catalog range.
        Returns:
            str: Delivery result.
        """

        speech = truncate_for_tts(text, runtime.config.tts_max_chars)
        if not speech:
            return "Speech text is empty"
        delivery_state = current_llm_chat_delivery()
        if delivery_state is not None:
            delivery_state = reserve_media_message()
        try:
            service = runtime.get_service()
            audio = await service.synthesize(
                speech,
                version=version,
                model_name=model_name,
                reference_language=reference_language,
                emotion=emotion,
                text_language=text_language,
                speed=speed,
            )
        except TTSSynthesisError as exc:
            return f"Speech synthesis failed: {exc}"
        except Exception:
            return "Speech synthesis service is unavailable"
        if not audio:
            return "Speech synthesis returned empty audio"
        await send_with_delivery(
            session,
            MessageChain([runtime.make_audio(audio, service.file_extension)]),
            delivery_state,
            media=True,
        )
        await runtime.append_history(session.channel.id, "", "bot", "assistant", f"[用语音说: {speech}]")
        return f"Speech sent: {speech}"

    return register_tool(dispatcher, speak)
