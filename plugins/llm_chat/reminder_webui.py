"""Entari WebUI extension for authenticated reminder task presentation."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from arclet.entari import plugin
from starlette.routing import BaseRoute
import entari_plugin_webui as webui_plugin  # entari: plugin
from entari_plugin_server import get_asgi
from arclet.entari.plugin.model import Plugin

from .reminder_webui_api import create_reminder_webui_router

_EXTENSION_ID = "llm_chat.reminders"
_PAGE_KEY = "llm-chat-reminders"
_MENU_PATH = f"/extension/{_PAGE_KEY}"
_COMPONENT_URL = "/api/llm-chat/reminders/page"
_ASSET_DIR = Path(__file__).with_name("webui_reminders")

plug = Plugin.current()


def _remove_registered_routes(app: FastAPI, routes: tuple[BaseRoute, ...]) -> None:
    for route in routes:
        if route in app.router.routes:
            app.router.routes.remove(route)


if plug is not None:
    app = get_asgi()
    if not isinstance(app, FastAPI):
        raise RuntimeError("Reminder WebUI requires entari-plugin-webui to own the FastAPI application")
    route_start = len(app.router.routes)
    app.include_router(create_reminder_webui_router(asset_dir=_ASSET_DIR))
    registered_routes = tuple(app.router.routes[route_start:])
    plugin.collect_disposes(lambda: _remove_registered_routes(app, registered_routes))

    extension = webui_plugin.webui_extend(_EXTENSION_ID)
    extension.menus[:] = [menu for menu in extension.menus if menu.path != _MENU_PATH]
    extension.pages[:] = [page for page in extension.pages if page.key != _PAGE_KEY]
    extension.add_menu("llm_chat.reminders.name", "mdi:bell-clock-outline", _MENU_PATH, order=47)
    extension.add_page(
        _PAGE_KEY,
        "llm_chat.reminders.name",
        "mdi:bell-clock-outline",
        _COMPONENT_URL,
        permission="llm_chat.reminders.view",
    )
    extension.add_i18n("zh-CN", "llm_chat.reminders.name", "提醒任务")
    extension.add_i18n("en-US", "llm_chat.reminders.name", "Reminders")
    extension.add_i18n("zh-CN", "llm_chat.reminders.permission.view", "查看提醒任务")
    extension.add_i18n("en-US", "llm_chat.reminders.permission.view", "View reminders")
    extension.add_permission("llm_chat.reminders.view", "llm_chat.reminders.permission.view")
    registered_menus = tuple(menu for menu in extension.menus if menu.path == _MENU_PATH)
    registered_pages = tuple(page for page in extension.pages if page.key == _PAGE_KEY)

    def _remove_extension_entries() -> None:
        extension.menus[:] = [menu for menu in extension.menus if menu not in registered_menus]
        extension.pages[:] = [page for page in extension.pages if page not in registered_pages]

    plugin.collect_disposes(_remove_extension_entries)
