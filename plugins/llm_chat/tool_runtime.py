"""Composition root for llm_chat LLM tool registration."""

from __future__ import annotations

from typing import cast
from datetime import datetime

from launart import Launart
from arclet.entari import Audio, Session, plugin, command, plugin_config
from entari_plugin_llm import LLMToolEvent  # entari: plugin
from arclet.entari.logger import log
from entari_plugin_database import get_session  # entari: plugin

from utils.path import AUDIO_DIR, IMAGE_DIR

from .config import LLMChatConfig
from .tools.web import register_web_access_tools
from .image_tags import pick_image
from .meme_store import import_meme_image
from .tools._tts import TTSServiceLike
from .tools.speak import SpeakToolContext, register_speak
from .chat_context import collect_message_images
from .core.delivery import (
    DeliveryError as DeliveryError,
    DeliveryState as DeliveryState,
    reserve_media_message as reserve_media_message,
)
from .persona.store import append_message
from .tools.support import audio_mime_type
from .tools.send_text import register_send_text
from .tools.tag_image import TagImageToolContext, register_tag_image
from .tools.send_audio import AudioToolContext, register_send_audio
from .tools.send_image import ImageToolContext, register_send_image
from .tools.call_plugin import CommandToolContext, register_call_plugin
from .tools._image_catalog import ImageCatalog
from .tools.get_local_time import LocalTimeToolContext, register_get_local_time
from .tools.list_tts_voices import TTSVoiceToolContext, register_list_tts_voices
from .tools.send_external_image import ExternalImageToolContext, register_send_external_image
from .tools.send_merged_forward import MergedForwardToolContext, register_send_merged_forward
from .tools.list_image_resources import register_list_image_resources

DINGGONG_DIR = AUDIO_DIR / "dinggong"
_LOGGER = log.wrapper("[llm_chat]")

config = plugin_config(LLMChatConfig)
tools = plugin.dispatch(LLMToolEvent)
registered_tools: list[str] = []

image_catalog = ImageCatalog(IMAGE_DIR, get_session)
image_context = ImageToolContext(
    config=config,
    catalog=image_catalog,
    pick_image=pick_image,
    append_history=append_message,
    warn=lambda message: _LOGGER.warning(message),
)
send_image = register_send_image(tools, image_context)
registered_tools.append("send_image")

send_text = register_send_text(tools)
registered_tools.append("send_text")

merged_forward_context = MergedForwardToolContext(warn=lambda message: _LOGGER.warning(message))
send_merged_forward = register_send_merged_forward(tools, merged_forward_context)
registered_tools.append("send_merged_forward")

list_image_resources = register_list_image_resources(tools, image_catalog)
registered_tools.append("list_image_resources")

external_image_context = ExternalImageToolContext(
    append_history=append_message,
    warn=lambda message: _LOGGER.warning(message),
)
send_external_image = register_send_external_image(tools, external_image_context)
registered_tools.append("send_external_image")

local_time_context = LocalTimeToolContext(now=datetime.now)
get_local_time = register_get_local_time(tools, local_time_context)
registered_tools.append("get_local_time")

audio_context = AudioToolContext(audio_dir=DINGGONG_DIR, append_history=append_message)
if registered := register_send_audio(tools, audio_context):
    send_audio = registered
    registered_tools.append("send_audio")


def _get_tts_service() -> TTSServiceLike:
    return cast(TTSServiceLike, Launart.current().get_component("tts.service"))


voice_catalog_context = TTSVoiceToolContext(
    enabled=config.tts_enabled,
    get_service=_get_tts_service,
)
if registered := register_list_tts_voices(tools, voice_catalog_context):
    list_tts_voices = registered
    registered_tools.append("list_tts_voices")

speak_context = SpeakToolContext(
    config=config,
    get_service=_get_tts_service,
    make_audio=lambda audio, suffix: Audio.of(raw=audio, mime=audio_mime_type(suffix)),
    append_history=append_message,
)
if registered := register_speak(tools, speak_context):
    speak = registered
    registered_tools.append("speak")


async def _execute_command(command_line: str, session: Session) -> object:
    return await command.execute(command_line, session)


command_context = CommandToolContext(
    allowed_commands=config.allowed_commands,
    execute=_execute_command,
    log_info=lambda message: _LOGGER.info(message),
)
if registered := register_call_plugin(tools, command_context):
    call_plugin = registered
    registered_tools.append("call_plugin")

registered_tools.extend(register_web_access_tools(tools, config))

tag_image_context = TagImageToolContext(
    config=config,
    collect_images=collect_message_images,
    import_image=import_meme_image,
)
tag_image = register_tag_image(tools, tag_image_context)
registered_tools.append("tag_image")
