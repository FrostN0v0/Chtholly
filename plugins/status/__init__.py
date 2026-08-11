"""Cute system status image plugin."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from arclet.entari import Session, MessageChain, BasicConfModel, command, metadata, plugin_config
from arclet.alconna import Alconna, CommandMeta
from satori.element import Image
from arclet.entari.logger import log
from arclet.entari.plugin import PluginRole, get_plugins

from utils.status_core import StatusCollectionError, collect_status, format_plain_status

from .render import StatusRenderError, render_status  # entari: subplugin


class StatusConfig(BasicConfModel):
    title: str = "Chtholly Status"
    subtitle: str = "A soft little window into the host"
    disk_path: str = "."
    sample_interval: float = 0.5


metadata(
    name="status",
    author=[{"name": "FrostN0v0"}],
    version="0.1.0",
    description="Render a live, pastel system status card.",
    role=PluginRole.NORMAL,
    config=StatusConfig,
)

config = plugin_config(StatusConfig)


def _framework_version() -> str:
    try:
        return version("arclet-entari")
    except PackageNotFoundError:
        return "unknown"


status_alconna = Alconna(
    "status",
    meta=CommandMeta(description="Render the current system status as an image"),
)
for shortcut in ("botstatus", "\u72b6\u6001", "\u8fd0\u884c\u72b6\u6001"):
    status_alconna.shortcut(
        shortcut,
        command="status",
        fuzzy=False,
        prefix=True,
        humanized=shortcut,
    )

status_dispatcher = command.mount(status_alconna)


@status_dispatcher.handle()
async def show_status(session: Session) -> None:
    try:
        snapshot = await collect_status(
            disk_path=config.disk_path,
            sample_interval=config.sample_interval,
            framework_version=_framework_version(),
            plugin_count=len(get_plugins()),
        )
    except StatusCollectionError:
        log.plugin.exception("status collection failed")
        await session.send("Status collection failed. Please try again later.")
        return

    fallback = format_plain_status(snapshot)
    try:
        png = await render_status(snapshot, title=config.title, subtitle=config.subtitle)
    except StatusRenderError:
        log.plugin.exception("status rendering failed, falling back to plain text")
        await session.send(fallback)
        return

    if not isinstance(png, bytes) or not png:
        log.plugin.warning("status rendering returned no image data")
        await session.send(fallback)
        return
    await session.send(MessageChain([Image.of(raw=png, mime="image/png")]))
