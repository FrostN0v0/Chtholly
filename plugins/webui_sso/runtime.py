"""Own native WebUI document augmentation and the reloadable SSO middleware."""

from __future__ import annotations

from pathlib import Path
from collections.abc import Callable
from importlib.resources import files

from fastapi import Depends, FastAPI, Request, APIRouter
from starlette.responses import Response, FileResponse, HTMLResponse
from starlette.middleware import Middleware
from entari_plugin_webui.api.deps import require_auth

from utils.webui_sso import SsoSessions, WebUISsoMiddleware

_CLIENT_PATH = "/api/webui-sso/client.js"
_HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}


def install(app: FastAPI, sessions: SsoSessions) -> Callable[[], None]:
    index = files("entari_plugin_webui").joinpath("static/frontend/index.html")
    script = Path(__file__).with_name("client.js")
    router = APIRouter()

    @router.get("/", include_in_schema=False)
    async def document(request: Request) -> Response:
        text = index.read_text(encoding="utf-8")
        if sessions.is_public(request):
            if "</head>" not in text:
                return Response("Unsupported WebUI document", status_code=503, headers=_HEADERS)
            text = text.replace("</head>", f'<script src="{_CLIENT_PATH}"></script></head>', 1)
        return HTMLResponse(text, headers=_HEADERS)

    @router.get(_CLIENT_PATH, include_in_schema=False, dependencies=[Depends(require_auth)])
    async def client() -> Response:
        return FileResponse(script, media_type="text/javascript", headers=_HEADERS)

    start = len(app.router.routes)
    app.include_router(router)
    registered = tuple(app.router.routes[start:])
    del app.router.routes[start:]
    app.router.routes[0:0] = registered
    middleware = Middleware(WebUISsoMiddleware, sessions=sessions)
    app.user_middleware.insert(0, middleware)
    app.middleware_stack = app.build_middleware_stack()
    app.openapi_schema = None

    def dispose() -> None:
        sessions.close()
        for route in registered:
            if route in app.router.routes:
                app.router.routes.remove(route)
        if middleware in app.user_middleware:
            app.user_middleware.remove(middleware)
            app.middleware_stack = app.build_middleware_stack()
        app.openapi_schema = None

    return dispose
