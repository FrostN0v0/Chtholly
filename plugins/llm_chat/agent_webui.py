"""Entari WebUI extension registration for AgentEvent sessions."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from arclet.entari import plugin, plugin_config
from starlette.routing import BaseRoute
import entari_plugin_webui as webui_plugin  # entari: plugin
from entari_plugin_server import get_asgi
from arclet.entari.plugin.model import Plugin
from entari_plugin_webui.api.deps import require_auth

from .config import LLMChatConfig
from .agent_admin import AgentAdminService
from .tool_runtime import registered_tool_schemas
from .agent_webui_api import create_agent_sessions_router

_EXTENSION_ID = "llm_chat.sessions"
_PAGE_KEY = "llm-chat-sessions"
_MENU_PATH = f"/extension/{_PAGE_KEY}"
_COMPONENT_URL = "/api/llm-chat/sessions/page"
_ASSET_DIR = Path(__file__).with_name("webui_sessions")

plug = Plugin.current()


def _remove_registered_routes(app: FastAPI, routes: tuple[BaseRoute, ...]) -> None:
    for route in routes:
        if route in app.router.routes:
            app.router.routes.remove(route)


if plug is not None:
    config = plugin_config(LLMChatConfig)
    service = AgentAdminService(config, registered_tool_schemas)
    router = create_agent_sessions_router(
        service,
        asset_dir=_ASSET_DIR,
        auth_dependency=require_auth,
    )

    app = get_asgi()
    if not isinstance(app, FastAPI):
        raise RuntimeError("Agent sessions WebUI requires entari-plugin-webui to own the FastAPI application")
    route_start = len(app.router.routes)
    app.include_router(router)
    registered_routes = tuple(app.router.routes[route_start:])
    plugin.collect_disposes(lambda: _remove_registered_routes(app, registered_routes))

    extension = webui_plugin.webui_extend(_EXTENSION_ID)
    extension.menus[:] = [menu for menu in extension.menus if menu.path != _MENU_PATH]
    extension.pages[:] = [page for page in extension.pages if page.key != _PAGE_KEY]
    extension.add_menu("llm_chat.sessions.name", "mdi:timeline-text-outline", _MENU_PATH, order=46)
    extension.add_page(
        _PAGE_KEY,
        "llm_chat.sessions.name",
        "mdi:timeline-text-outline",
        _COMPONENT_URL,
        permission="llm_chat.sessions.manage",
    )
    extension.add_i18n("zh-CN", "llm_chat.sessions.name", "LLM 会话")
    extension.add_i18n("en-US", "llm_chat.sessions.name", "LLM Sessions")
    extension.add_i18n("zh-CN", "llm_chat.sessions.permission.manage", "管理 LLM 会话")
    extension.add_i18n("en-US", "llm_chat.sessions.permission.manage", "Manage LLM sessions")
    extension.add_permission("llm_chat.sessions.manage", "llm_chat.sessions.permission.manage")
    registered_menus = tuple(menu for menu in extension.menus if menu.path == _MENU_PATH)
    registered_pages = tuple(page for page in extension.pages if page.key == _PAGE_KEY)

    def _remove_extension_entries() -> None:
        extension.menus[:] = [menu for menu in extension.menus if menu not in registered_menus]
        extension.pages[:] = [page for page in extension.pages if page not in registered_pages]

    plugin.collect_disposes(_remove_extension_entries)
