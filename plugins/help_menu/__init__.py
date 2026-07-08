"""Visual help menu plugin.

Auto-generates an image menu from loaded plugins & their commands,
rendered with Tailwind CSS v4 (precompiled `templates/index.css`).

Regenerate the stylesheet after editing templates:
    pnpm run build
"""

import time
import hashlib
from dataclasses import field

from arclet.entari import Image, Session, MessageChain, BasicConfModel, command, metadata, local_data, plugin_config
from arclet.entari.plugin import PluginRole
from arclet.entari.command import Match

# entari: plugin
import entari_plugin_browser as _browser  # noqa: F401  (hard dep: rendering backend)

from .render import render_menu
from .collect import collect_entries

metadata(
    name="help_menu",
    author=[{"name": "FrostN0v0"}],
    version="0.1.0",
    description="图片帮助菜单：自动汇总插件与指令",
    role=PluginRole.NORMAL,
)


class HelpMenuConfig(BasicConfModel):
    show_hidden: bool = False
    columns: int = 2
    title: str = "Chtholly 功能菜单"
    custom_icons: dict[str, str] = field(default_factory=dict)
    cache_ttl: int = 600
    """Rendered image cache lifetime in seconds; 0 disables caching."""


config = plugin_config(HelpMenuConfig)

HELP_META = {"icon": "\U0001f4d6", "category": "基础"}


def _cache_file(digest: str):
    return local_data.get_cache_dir("help_menu") / f"{digest}.png"


def _digest(grouped) -> str:
    parts = [f"{config.title}|{config.columns}|{config.show_hidden}"]
    for category, entries in grouped.items():
        for e in entries:
            parts.append(f"{category}/{e.icon}/{e.name}/{e.version}/{','.join(e.commands)}")
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _plain_fallback(grouped) -> str:
    lines: list[str] = []
    for category, entries in grouped.items():
        lines.append(f"【{category}】")
        for e in entries:
            desc = f" - {e.description}" if e.description else ""
            lines.append(f"  {e.name}{desc}")
            lines.extend(f"    {cmd}" for cmd in e.commands)
    return "\n".join(lines)


# NOTE: command.on() uses `{name}` format grammar; `[optional]` only works
# with the AlconnaString grammar used by command.command().
@command.command("help [category]", "生成图片帮助菜单")
async def help_menu(session: Session, category: Match[str]):
    """生成图片帮助菜单"""
    selected = category.result if category.available else None
    grouped = collect_entries(show_hidden=config.show_hidden, custom_icons=config.custom_icons)
    if selected:
        grouped = {k: v for k, v in grouped.items() if k == selected}
    if not grouped:
        await session.send("当前没有可展示的插件")
        return

    cache = _cache_file(_digest(grouped))
    if config.cache_ttl > 0 and cache.exists() and time.time() - cache.stat().st_mtime < config.cache_ttl:
        await session.send(MessageChain([Image.of(path=cache)]))
        return

    try:
        png = await render_menu(grouped, title=config.title, columns=config.columns)
    except Exception:
        from arclet.entari.logger import log

        log.plugin.exception("help menu render failed, falling back to plain text")
        await session.send(_plain_fallback(grouped))
        return

    if config.cache_ttl > 0:
        cache.write_bytes(png)
    await session.send(MessageChain([Image.of(raw=png, mime="image/png")]))
