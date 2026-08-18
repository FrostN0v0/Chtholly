"""Entari WebUI extension registration for meme administration."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from arclet.entari import plugin
from starlette.routing import BaseRoute
import entari_plugin_webui as webui_plugin  # entari: plugin
from arclet.entari.plugin import plugin_config
from entari_plugin_server import get_asgi
from arclet.entari.plugin.model import Plugin
from entari_plugin_webui.api.deps import require_auth

from .config import LLMChatConfig
from .meme_admin import MemeAdminService
from .meme_webui_api import create_meme_admin_router

_EXTENSION_ID = "llm_chat.memes"
_PAGE_KEY = "llm-chat-memes"
_MENU_PATH = f"/extension/{_PAGE_KEY}"
_COMPONENT_URL = "/api/llm-chat/memes/page"
_ASSET_DIR = Path(__file__).with_name("webui")

plug = Plugin.current()


def _remove_registered_routes(app: FastAPI, routes: tuple[BaseRoute, ...]) -> None:
    for route in routes:
        if route in app.router.routes:
            app.router.routes.remove(route)


if plug is not None:
    config = plugin_config(LLMChatConfig)
    service = MemeAdminService(config)
    router = create_meme_admin_router(
        service,
        asset_dir=_ASSET_DIR,
        auth_dependency=require_auth,
    )

    app = get_asgi()
    if not isinstance(app, FastAPI):
        raise RuntimeError("Meme WebUI requires entari-plugin-webui to own the FastAPI application")
    route_start = len(app.router.routes)
    app.include_router(router)
    registered_routes = tuple(app.router.routes[route_start:])
    plugin.collect_disposes(lambda: _remove_registered_routes(app, registered_routes))

    extension = webui_plugin.webui_extend(_EXTENSION_ID)
    extension.menus[:] = [menu for menu in extension.menus if menu.path != _MENU_PATH]
    extension.pages[:] = [page for page in extension.pages if page.key != _PAGE_KEY]
    extension.add_menu("llm_chat.memes.name", "mdi:sticker-emoji", _MENU_PATH, order=45)
    extension.add_page(
        _PAGE_KEY,
        "llm_chat.memes.name",
        "mdi:sticker-emoji",
        _COMPONENT_URL,
        permission="llm_chat.memes.manage",
    )
    extension.add_i18n("zh-CN", "llm_chat.memes.name", "Meme Library")
    extension.add_i18n("en-US", "llm_chat.memes.name", "Meme Library")
    extension.add_i18n("zh-CN", "llm_chat.memes.permission.manage", "Manage meme library")
    extension.add_i18n("en-US", "llm_chat.memes.permission.manage", "Manage meme library")
    extension.add_permission("llm_chat.memes.manage", "llm_chat.memes.permission.manage")
    registered_menus = tuple(menu for menu in extension.menus if menu.path == _MENU_PATH)
    registered_pages = tuple(page for page in extension.pages if page.key == _PAGE_KEY)

    def _remove_extension_entries() -> None:
        extension.menus[:] = [menu for menu in extension.menus if menu not in registered_menus]
        extension.pages[:] = [page for page in extension.pages if page not in registered_pages]

    plugin.collect_disposes(_remove_extension_entries)
