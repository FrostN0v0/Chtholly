"""LLM tool and administrator command for meme collection."""

from __future__ import annotations

import asyncio

from satori import Text
from arclet.entari import Image, Session, MessageChain, plugin, command, plugin_config
from arclet.alconna import Arg, AllParam
from arclet.letoderea import STOP
from entari_plugin_llm import LLMToolEvent  # entari: plugin
from arclet.entari.filter import superusers
from arclet.entari.logger import log
from arclet.entari.command import Match

from .config import LLMChatConfig
from .core.media import format_meme_collection_record
from .meme_store import MemeImportError, MemeImportResult, import_meme_image
from .core.errors import summarize_exception
from .chat_context import collect_quoted_images, collect_message_images
from .core.delivery import current_llm_chat_delivery
from .persona.store import append_message

config = plugin_config(LLMChatConfig)
tools = plugin.dispatch(LLMToolEvent)
registered_tools = ["tag_image"]
_superuser_check = superusers().check
_LOGGER = log.wrapper("[llm_chat]")


async def _remember_collection(session: Session, result: MemeImportResult) -> None:
    record = format_meme_collection_record(result.relative_path, result.tags)
    task = asyncio.create_task(append_message(session.channel.id, "", "bot", "assistant", record))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        except Exception as exc:
            _LOGGER.warning(f"meme collection history failed: {summarize_exception(exc)}")
        raise
    except Exception as exc:
        _LOGGER.warning(f"meme collection history failed: {summarize_exception(exc)}")


@tools
async def tag_image(session: Session, image_index: int = 1) -> str:
    """Collect one reusable meme, reaction image, or sticker from the current direct or replied images.

    Use only for an image already attached directly to the current message or its hydrated reply when it is clearly
    reusable as an emotional reaction, reply scene, sticker, or meme. image_index is a 1-based index over all direct
    images first and then all replied images. Never use this for generated or sent images, bare unavailable markers,
    ordinary or sensitive images, or images inside forwarded messages.

    Args:
        image_index (int): Optional 1-based direct-then-replied image index. Defaults to 1.
    Returns:
        str: Privacy-safe collection status without paths, tags, hashes, or database details.
    """
    if current_llm_chat_delivery() is None:
        raise MemeImportError("Image collection is unavailable outside an active llm_chat generation")
    if type(image_index) is not int or image_index < 1:
        raise MemeImportError("image_index must be a positive 1-based integer")

    candidates = collect_message_images(session)
    if image_index > len(candidates):
        raise MemeImportError("image_index does not identify a current direct or replied image")

    result = await import_meme_image(config, session, candidates[image_index - 1][0])
    await _remember_collection(session, result)
    if result.status == "created":
        return "Collected the current image as a reusable meme."
    if result.status == "duplicate":
        return "The current image is already in the meme collection."
    return "The existing meme now has searchable tags."


def _select_command_image(payload: Match[MessageChain], session: Session) -> tuple[Image | None, str | None]:
    elements = payload.result if payload.available else MessageChain()
    direct_images: list[Image] = []
    for element in elements:
        if isinstance(element, Text):
            if element.text.strip():
                return None, "Manual tags or extra text are not accepted; attach only one image."
            continue
        if not isinstance(element, Image):
            return None, "Only one direct image or one replied image can be collected."
        direct_images.append(element)

    if len(direct_images) > 1:
        return None, "Only one image can be collected at a time."
    if direct_images:
        return direct_images[0], None

    quoted_images = collect_quoted_images(session)
    if not quoted_images:
        return None, "Attach one image after the command or reply to a message containing one image."
    if len(quoted_images) > 1:
        return None, "Only one image can be collected at a time."
    return quoted_images[0], None


def _format_command_result(result: MemeImportResult) -> str:
    if result.status == "created":
        status = "Collected"
    elif result.status == "duplicate":
        status = "Already collected"
    else:
        status = "Tagged existing image"
    return f"{status}: {result.relative_path}; automatic tags: {result.tags}"


@command.on("llmchat tag-meme {payload}", args={"payload": Arg("payload?", AllParam)})
async def tag_meme_cmd(session: Session, payload: Match[MessageChain]) -> str:
    if await _superuser_check(session) is STOP:
        return "Permission denied: only configured superusers may collect memes."

    image, error = _select_command_image(payload, session)
    if error is not None:
        return error
    assert image is not None
    try:
        result = await import_meme_image(config, session, image)
        await _remember_collection(session, result)
    except asyncio.CancelledError:
        raise
    except MemeImportError as exc:
        return f"Meme collection failed: {exc}"
    return _format_command_result(result)
