"""Command and startup runtime for local image tagging."""

from __future__ import annotations

import asyncio

from arclet.entari import Session, plugin, command, scheduler, plugin_config
from arclet.entari.filter import superusers
from arclet.entari.plugin.model import Plugin

from .config import LLMChatConfig
from .image_tags import ProgressReporter, tag_images
from .persona.store import nightly_decay

_active_tag_pass: asyncio.Task[tuple[int, int, int]] | None = None
_active_tag_scope: str | None = None


def _cancel_active_tag_pass() -> None:
    if _active_tag_pass is not None and not _active_tag_pass.done():
        _active_tag_pass.cancel()


plugin.collect_disposes(_cancel_active_tag_pass)


async def _progress_reporter(config: LLMChatConfig, session: Session, scope: str) -> ProgressReporter:
    """Return an async callback that sends tagging progress to the session."""

    async def report(tagged: int, failed: int, total: int) -> None:
        done = tagged + failed
        if total == 0:
            await session.send(f"{scope}：没有需要处理的图片。")
        elif done == 0:
            concurrency = max(1, config.tag_concurrency)
            est_min = max(1, round(total * 3 / concurrency / 60))
            await session.send(f"{scope}：共 {total} 张，并发 {concurrency}，预计约 {est_min} 分钟。")
        elif done >= total:
            await session.send(f"{scope}完成：成功 {tagged}，失败 {failed}。")
        else:
            await session.send(f"{scope}进度：{done}/{total}（失败 {failed}）")

    return report


async def _launch_tag_pass(
    config: LLMChatConfig,
    scope: str,
    limit: int | None,
    *,
    retag: bool,
    session: Session | None = None,
) -> str:
    """Start an exclusive tagging pass, cancelling any pass already running."""
    global _active_tag_pass, _active_tag_scope
    cancelled = None
    if _active_tag_pass is not None and not _active_tag_pass.done():
        cancelled = _active_tag_scope
        _active_tag_pass.cancel()
        try:
            await _active_tag_pass
        except asyncio.CancelledError:
            pass
    on_progress = await _progress_reporter(config, session, scope) if session is not None else None
    _active_tag_scope = scope
    _active_tag_pass = asyncio.create_task(tag_images(config, limit, retag=retag, on_progress=on_progress))
    if cancelled:
        return f"已终止运行中的「{cancelled}」任务，开始{scope}，"
    return f"已开始{scope}，"


config = plugin_config(LLMChatConfig)
plug = Plugin.current()


@plug.use("::startup")
async def tag_images_on_startup() -> None:
    if not config.image_tags_enabled:
        return
    await _launch_tag_pass(config, "启动增量标注", config.tag_batch_size, retag=False)


@command.on("llmchat retag-images")
@superusers()
async def retag_images(session: Session) -> None:
    status = await _launch_tag_pass(config, "重标 50 张", 50, retag=True, session=session)
    await session.send(status + "进度稍后报告。")


@command.on("llmchat tag-images")
@superusers()
async def tag_images_cmd(session: Session) -> None:
    status = await _launch_tag_pass(config, "增量标注", None, retag=False, session=session)
    await session.send(status + "进度稍后报告。")


@command.on("llmchat retag-images-all")
@superusers()
async def retag_images_all(session: Session) -> None:
    status = await _launch_tag_pass(config, "全量重标", None, retag=True, session=session)
    await session.send(status + "进度稍后报告。")


@scheduler.cron("0 4 * * *")
async def nightly_decay_job() -> None:
    await nightly_decay()
