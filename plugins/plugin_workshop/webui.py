"""Register the workshop inside the existing Entari WebUI lifecycle."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from arclet.entari import plugin
from starlette.routing import BaseRoute

from utils.plugin_workshop_core.models import WorkshopAPI

from .webui_api import create_workshop_router  # entari: package
from .webui_auth import register_password_authentication  # entari: package


def _remove_routes(app: FastAPI, registered: tuple[BaseRoute, ...]) -> None:
    app.router.routes[:] = [route for route in app.router.routes if not any(route is owned for owned in registered)]


def register_webui(service: WorkshopAPI) -> None:
    import entari_plugin_webui as webui_plugin  # entari: plugin
    from entari_plugin_server import get_asgi

    register_password_authentication()

    app = get_asgi()
    if not isinstance(app, FastAPI):
        raise RuntimeError("Plugin workshop requires the Entari WebUI FastAPI application")
    router = create_workshop_router(service, asset_dir=Path(__file__).with_name("webui"))
    start = len(app.router.routes)
    app.include_router(router)
    registered = tuple(app.router.routes[start:])
    plugin.collect_disposes(lambda: _remove_routes(app, registered))

    extension = webui_plugin.webui_extend("plugin_workshop")
    menu_path = "/extension/plugin-workshop"
    page_key = "plugin-workshop"
    extension.menus[:] = [menu for menu in extension.menus if menu.path != menu_path]
    extension.pages[:] = [page for page in extension.pages if page.key != page_key]
    extension.add_menu("plugin_workshop.name", "mdi:puzzle-edit-outline", menu_path, order=47)
    extension.add_page(
        page_key,
        "plugin_workshop.name",
        "mdi:puzzle-edit-outline",
        "/api/plugin-workshop/page",
        permission="plugin_workshop.manage",
    )
    extension.add_i18n("zh-CN", "plugin_workshop.name", "插件工坊")
    extension.add_i18n("en-US", "plugin_workshop.name", "Plugin Workshop")
    extension.add_i18n("zh-CN", "plugin_workshop.permission.manage", "审核与管理原生插件")
    extension.add_i18n("en-US", "plugin_workshop.permission.manage", "Review and manage native plugins")
    extension.add_permission("plugin_workshop.manage", "plugin_workshop.permission.manage")
    menus = tuple(menu for menu in extension.menus if menu.path == menu_path)
    pages = tuple(page for page in extension.pages if page.key == page_key)

    def dispose_entries() -> None:
        extension.menus[:] = [menu for menu in extension.menus if not any(menu is owned for owned in menus)]
        extension.pages[:] = [page for page in extension.pages if not any(page is owned for owned in pages)]

    plugin.collect_disposes(dispose_entries)
