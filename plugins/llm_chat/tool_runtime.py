"""Composition root for llm_chat LLM tool registration."""

from __future__ import annotations

from typing import cast
from pathlib import Path
from datetime import datetime

from launart import Launart
import litellm
from arclet.entari import Audio, Session, plugin, command, plugin_config
from entari_plugin_llm import LLMToolEvent  # entari: plugin
from arclet.entari.logger import log
import entari_plugin_browser as _browser  # entari: plugin  # noqa: F401
from entari_plugin_browser import PlaywrightService
from entari_plugin_database import get_session  # entari: plugin
import entari_plugin_htmlrender as _htmlrender  # entari: plugin  # noqa: F401
from entari_plugin_htmlrender import HtmlRenderer
from entari_plugin_llm.config import get_model_list, get_model_config
from entari_plugin_llm.exception import ModelNotFoundError
from entari_plugin_htmlrender.entari import HtmlRenderService

from utils.path import AUDIO_DIR, IMAGE_DIR

from .config import LLMChatConfig
from .tools.web import register_web_access_tools
from .image_tags import pick_image
from .meme_store import import_meme_image
from .perception import get_channel_perception
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
from .tools.html2pic import register_html2pic
from .tools.jinja2pic import register_jinja2pic
from .tools.send_text import SendTextToolContext, register_send_text
from .tools.tag_image import TagImageToolContext, register_tag_image
from .tools._rendering import RenderToolContext
from .tools.send_audio import AudioToolContext, register_send_audio
from .tools.send_image import ImageToolContext, register_send_image
from .tools.call_plugin import CommandToolContext, register_call_plugin
from .tools.markdown2pic import register_markdown2pic
from .tools._image_catalog import ImageCatalog
from .tools.generate_image import ImageGenerationToolContext, register_generate_image
from .tools.get_local_time import LocalTimeToolContext, register_get_local_time
from .tools.list_tts_voices import TTSVoiceToolContext, register_list_tts_voices
from .tools.send_channel_image import ChannelImageToolContext, register_send_channel_image
from .tools.screenshot_web_page import (
    WebScreenshotToolContext,
    register_screenshot_web_page,
)
from .tools.send_external_image import ExternalImageToolContext, register_send_external_image
from .tools.send_merged_forward import MergedForwardToolContext, register_send_merged_forward
from .tools.list_image_resources import register_list_image_resources
from .tools.read_channel_messages import register_read_channel_messages
from .tools.describe_channel_image import ChannelImageDescriptionContext, register_describe_channel_image
from .tools.find_channel_participants import register_find_channel_participants
from .tools.describe_channel_participant_avatar import register_describe_channel_participant_avatar

DINGGONG_DIR = AUDIO_DIR / "dinggong"
RENDER_TEMPLATE_DIR = Path(__file__).resolve().parent / "render_templates"
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


async def _resolve_text_mention(session: Session, participant_ref: str):
    return await get_channel_perception().refresh_participant(session, participant_ref)


send_text_context = SendTextToolContext(resolve_participant=_resolve_text_mention)
send_text = register_send_text(tools, send_text_context)
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


def _resolve_image_generation_model(channel_id: str):
    model_name = config.image_generation_model
    if not model_name or model_name not in get_model_list():
        raise ModelNotFoundError(f"Image generation model {model_name!r} is not configured")
    return get_model_config(model_name, channel_id)


async def _generate_image_provider(**kwargs: object) -> object:
    return await litellm.aimage_generation(**kwargs)


if config.image_generation_model:
    image_generation_context = ImageGenerationToolContext(
        resolve_model=_resolve_image_generation_model,
        generate=_generate_image_provider,
        append_history=append_message,
        warn=lambda message: _LOGGER.warning(message),
        timeout_seconds=max(1.0, float(config.image_generation_timeout)),
        quality=config.image_generation_quality,
        output_format=config.image_generation_output_format,
        output_compression=config.image_generation_output_compression,
    )
    generate_image = register_generate_image(tools, image_generation_context)
    registered_tools.append("generate_image")


def _get_html_renderer() -> HtmlRenderer:
    service = cast(HtmlRenderService, Launart.current().get_component("htmlrender.runtime"))
    return service.renderer


render_context = RenderToolContext(
    get_renderer=_get_html_renderer,
    append_history=append_message,
    warn=lambda message: _LOGGER.warning(message),
    template_root=RENDER_TEMPLATE_DIR,
)
markdown2pic = register_markdown2pic(tools, render_context)
registered_tools.append("markdown2pic")
html2pic = register_html2pic(tools, render_context)
registered_tools.append("html2pic")
jinja2pic = register_jinja2pic(tools, render_context)
registered_tools.append("jinja2pic")


def _get_browser_service() -> PlaywrightService:
    return cast(PlaywrightService, Launart.current().get_component("web.render/playwright"))


web_screenshot_context = WebScreenshotToolContext(
    get_browser=_get_browser_service,
    append_history=append_message,
    warn=lambda message: _LOGGER.warning(message),
    read_limit=max(0, config.web_page_max_calls_per_generation),
    total_limit=max(0, config.web_total_max_calls_per_generation),
)
if config.web_page_max_calls_per_generation > 0 and config.web_total_max_calls_per_generation > 0:
    screenshot_web_page = register_screenshot_web_page(tools, web_screenshot_context)
    registered_tools.append("screenshot_web_page")

local_time_context = LocalTimeToolContext(now=datetime.now)
get_local_time = register_get_local_time(tools, local_time_context)
registered_tools.append("get_local_time")

find_channel_participants = register_find_channel_participants(tools, get_channel_perception)
registered_tools.append("find_channel_participants")
read_channel_messages = register_read_channel_messages(
    tools,
    get_channel_perception,
    config,
)
registered_tools.append("read_channel_messages")
channel_image_description_context = ChannelImageDescriptionContext(
    config=config,
    get_perception=get_channel_perception,
)
describe_channel_image = register_describe_channel_image(tools, channel_image_description_context)
registered_tools.append("describe_channel_image")


channel_image_context = ChannelImageToolContext(
    get_perception=get_channel_perception,
    append_history=append_message,
    warn=_LOGGER.warning,
)
send_channel_image = register_send_channel_image(tools, channel_image_context)
registered_tools.append("send_channel_image")

describe_channel_participant_avatar = register_describe_channel_participant_avatar(
    tools,
    get_channel_perception,
    config,
)
registered_tools.append("describe_channel_participant_avatar")

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
