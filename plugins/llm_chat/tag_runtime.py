"""Startup image tagging and scheduled relationship maintenance."""

from __future__ import annotations

import asyncio
from contextlib import suppress

from arclet.entari import plugin, scheduler, plugin_config
from arclet.entari.plugin.model import Plugin

from .config import LLMChatConfig
from .image_tags import tag_images
from .persona.store import nightly_decay

_active_tag_pass: asyncio.Task[tuple[int, int, int]] | None = None


def _cancel_active_tag_pass() -> None:
    if _active_tag_pass is not None and not _active_tag_pass.done():
        _active_tag_pass.cancel()


plugin.collect_disposes(_cancel_active_tag_pass)
config = plugin_config(LLMChatConfig)
plug = Plugin.current()


@plug.use("::startup")
async def tag_images_on_startup() -> None:
    global _active_tag_pass
    if not config.image_tags_enabled:
        return
    if _active_tag_pass is not None and not _active_tag_pass.done():
        _active_tag_pass.cancel()
        with suppress(asyncio.CancelledError):
            await _active_tag_pass
    _active_tag_pass = asyncio.create_task(tag_images(config, config.tag_batch_size, retag=False))


@scheduler.cron("0 4 * * *")
async def nightly_decay_job() -> None:
    await nightly_decay()
