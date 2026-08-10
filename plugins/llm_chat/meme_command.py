"""Administrator command for manual meme collection."""

from __future__ import annotations

import asyncio

from satori import Text
from arclet.entari import Image, Session, MessageChain, command, plugin_config
from arclet.alconna import Arg, AllParam
from arclet.letoderea import STOP
from arclet.entari.filter import superusers
from arclet.entari.command import Match

from .config import LLMChatConfig
from .meme_store import MemeImportError, MemeImportResult, import_meme_image
from .chat_context import collect_quoted_images

config = plugin_config(LLMChatConfig)
_superuser_check = superusers().check


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
    except asyncio.CancelledError:
        raise
    except MemeImportError as exc:
        return f"Meme collection failed: {exc}"
    return _format_command_result(result)
