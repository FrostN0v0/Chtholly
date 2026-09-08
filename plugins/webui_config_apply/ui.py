"""Lifecycle-owned WebUI assets, navigation, and native settings feedback."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Request, APIRouter, HTTPException
from starlette.types import Send, Scope, ASGIApp, Message, Receive
import entari_plugin_webui as webui_plugin  # entari: plugin
from starlette.responses import FileResponse
from starlette.middleware import Middleware
from arclet.entari.plugin.model import Plugin
from entari_plugin_webui.api.deps import require_auth

_ASSETS = Path(__file__).with_name("frontend")
_PREFIX = "/api/config-apply"
_PAGE_KEY = "configuration-apply"
_MENU_PATH = f"/extension/{_PAGE_KEY}"
_SCRIPT = b'<script src="/api/config-apply/assets/panel.js" defer></script>'
_ASSET_TYPES = {"panel.js": "text/javascript", "panel.css": "text/css"}
_PAGE_HEADERS = {
    "Cache-Control": "private, no-store",
    "X-Content-Type-Options": "nosniff",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'self'"
    ),
}


def _same_origin_asset(request: Request) -> None:
    if request.headers.get("sec-fetch-site") in {"cross-site", "same-site"}:
        raise HTTPException(403, "Same-origin access is required")
    origin = request.headers.get("origin") or request.headers.get("referer")
    if origin:
        try:
            source = urlsplit(origin)
            expected = urlsplit(str(request.base_url))
            matches = (source.scheme, source.netloc) == (expected.scheme, expected.netloc)
        except ValueError:
            matches = False
        if not matches:
            raise HTTPException(403, "Same-origin access is required")


class _NativePanelInjection:
    """Modify only a small, complete native HTML response, before compression."""

    def __init__(self, app: ASGIApp, enabled: list[bool]) -> None:
        self.app = app
        self.enabled = enabled

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            not self.enabled[0]
            or scope["type"] != "http"
            or scope.get("method") != "GET"
            or scope.get("path") not in {"/", "/index.html"}
        ):
            await self.app(scope, receive, send)
            return
        # FileResponse must send bytes, not an opaque server-side path. Never
        # change sendfile behavior for assets, APIs, or extension iframe pages.
        scope = dict(scope)
        scope["extensions"] = {
            key: value for key, value in scope.get("extensions", {}).items() if key != "http.response.pathsend"
        }
        start: Message | None = None

        async def inject(message: Message) -> None:
            nonlocal start
            if message["type"] == "http.response.start":
                headers = dict(message.get("headers", []))
                if (
                    message["status"] == 200
                    and headers.get(b"content-type", b"").split(b";", 1)[0].strip() == b"text/html"
                    and headers.get(b"content-encoding", b"identity") == b"identity"
                    and b"content-range" not in headers
                    and not message.get("trailers")
                ):
                    start = message
                    return
            if start is not None:
                body = message.get("body", b"")
                # Streaming bodies pass through immediately without accumulating
                # chunks. This middleware is innermost, before BaseHTTPMiddleware.
                if (
                    message["type"] == "http.response.body"
                    and not message.get("more_body", False)
                    and len(body) <= 262144
                    and _SCRIPT not in body
                ):
                    position = body.lower().find(b"</head>")
                    if position >= 0:
                        body = body[:position] + _SCRIPT + body[position:]
                        headers = [
                            (key, value)
                            for key, value in start.get("headers", [])
                            if key.lower()
                            not in {
                                b"content-length",
                                b"etag",
                                b"last-modified",
                                b"cache-control",
                                b"content-md5",
                                b"digest",
                                b"content-digest",
                                b"repr-digest",
                            }
                        ]
                        headers.extend(
                            [
                                (b"content-length", str(len(body)).encode("ascii")),
                                (b"cache-control", b"private, no-store"),
                            ]
                        )
                        start = {**start, "headers": headers}
                        message = {**message, "body": body}
                await send(start)
                start = None
            await send(message)

        await self.app(scope, receive, inject)


def install_ui(app: FastAPI, plug: Plugin) -> None:
    router = APIRouter(
        prefix=_PREFIX,
        dependencies=[Depends(require_auth), Depends(_same_origin_asset)],
    )

    @router.get("/page", include_in_schema=False)
    async def page() -> FileResponse:
        return FileResponse(_ASSETS / "index.html", media_type="text/html", headers=_PAGE_HEADERS)

    @router.get("/assets/{file}", include_in_schema=False)
    async def asset(file: str) -> FileResponse:
        media_type = _ASSET_TYPES.get(file)
        if media_type is None:
            raise HTTPException(404, "Asset not found")
        return FileResponse(
            _ASSETS / file,
            media_type=media_type,
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
                "Cross-Origin-Resource-Policy": "same-origin",
            },
        )

    route_start = len(app.router.routes)
    app.include_router(router)
    routes = tuple(app.router.routes[route_start:])
    extension = webui_plugin.webui_extend("webui_config_apply")
    extension.add_menu("webui_config_apply.name", "mdi:restart", _MENU_PATH, order=47)
    menu = extension.menus[-1]
    extension.add_page(_PAGE_KEY, "webui_config_apply.name", "mdi:restart", f"{_PREFIX}/page")
    page_entry = extension.pages[-1]
    extension.add_i18n("zh-CN", "webui_config_apply.name", "配置应用与重启")
    extension.add_i18n("en-US", "webui_config_apply.name", "Configuration Apply")

    # This WebUI version exposes menus/pages but no frontend script hook.
    # Install innermost: outer security/CSP and compression remain authoritative.
    enabled = [True]
    middleware = Middleware(_NativePanelInjection, enabled=enabled)
    app.user_middleware.append(middleware)
    app.middleware_stack = None

    def dispose() -> None:
        enabled[0] = False
        app.router.routes[:] = [route for route in app.router.routes if not any(route is own for own in routes)]
        extension.menus[:] = [item for item in extension.menus if item is not menu]
        extension.pages[:] = [item for item in extension.pages if item is not page_entry]
        app.user_middleware[:] = [item for item in app.user_middleware if item is not middleware]
        app.middleware_stack = None

    plug.collect(dispose)
