"""Authenticated route overrides in the existing WebUI FastAPI application."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit
from collections.abc import Callable

from fastapi import Depends, FastAPI, Request, APIRouter, HTTPException
from fastapi.responses import JSONResponse
from arclet.entari.config.file import EntariConfig
from entari_plugin_webui.api.deps import require_auth

from utils.webui_config_core import ConfigValidationError

from .saving import SaveError, ConfigSaver
from .control import read_status, status_payload, request_restart


def require_same_origin(request: Request) -> None:
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        if request.method in {"GET", "HEAD", "OPTIONS"}:
            return
        raise HTTPException(status_code=403, detail="Same-origin request required")
    try:
        parsed = urlsplit(origin)
        if parsed.scheme == request.url.scheme and parsed.netloc == request.headers.get("host"):
            return
    except ValueError:
        pass
    raise HTTPException(status_code=403, detail="Same-origin request required")


async def _payload(request: Request, field: str, expected: type | tuple[type, ...]) -> Any:
    try:
        payload = json.loads(await request.body())
    except (ValueError, UnicodeDecodeError):
        raise ConfigValidationError("Request body must be valid JSON", code="invalid_request") from None
    if not isinstance(payload, dict) or field not in payload or not isinstance(payload[field], expected):
        raise ConfigValidationError("Request body has the wrong configuration shape", code="invalid_request")
    return payload[field]


def _error(exc: Exception) -> JSONResponse:
    if isinstance(exc, ConfigValidationError):
        status, code, message = 400, exc.code, str(exc)
    elif isinstance(exc, SaveError):
        status, code, message = exc.status, exc.code, str(exc)
    elif isinstance(exc, OSError):
        status, code, message = 503, "config_io_failed", "Configuration files are unavailable or not writable"
    else:
        # Native parser/serializer exceptions can include credentials and source
        # lines. Never forward them to WebUI's generic exception logger/response.
        status, code, message = (
            400,
            "config_save_failed",
            "Configuration could not be prepared safely; reload the page and review its structure",
        )
    return JSONResponse(
        {"success": False, "code": code, "message": message}, status_code=status, headers={"Cache-Control": "no-store"}
    )


def install_api(app: FastAPI, running_sha256: str | None) -> Callable[[], None]:
    saver = ConfigSaver(running_sha256)
    router = APIRouter(dependencies=[Depends(require_auth), Depends(require_same_origin)])

    @router.put("/api/plugins/{plugin_id}/config")
    async def save_plugin(plugin_id: str, request: Request):
        try:
            if not read_status()[0]:
                raise SaveError(
                    "The automatic configuration helper is unavailable", code="helper_unavailable", status=503
                )
            value = await _payload(request, "config", dict)
            return JSONResponse(saver.save_plugin(plugin_id, value), headers={"Cache-Control": "no-store"})
        except Exception as exc:
            return _error(exc)

    @router.put("/api/config/{section}")
    async def save_section(section: str, request: Request):
        try:
            if not read_status()[0]:
                raise SaveError(
                    "The automatic configuration helper is unavailable", code="helper_unavailable", status=503
                )
            value = await _payload(request, "data", (dict, list))
            result = saver.save_section(section, value)
            return JSONResponse({**result, "message": "已保存"}, headers={"Cache-Control": "no-store"})
        except Exception as exc:
            return _error(exc)

    @router.get("/api/config-apply/status")
    async def status():
        try:
            return JSONResponse(
                status_payload(EntariConfig.instance.path, running_sha256), headers={"Cache-Control": "no-store"}
            )
        except Exception as exc:
            return _error(exc)

    @router.post("/api/config-apply/restart")
    async def restart():
        try:
            with saver.lock:
                request_id = request_restart()
            return JSONResponse({"success": True, "request_id": request_id}, headers={"Cache-Control": "no-store"})
        except Exception as exc:
            return _error(exc)

    route_start = len(app.router.routes)
    app.include_router(router)
    registered = tuple(app.router.routes[route_start:])
    del app.router.routes[route_start:]
    app.router.routes[0:0] = registered
    app.openapi_schema = None

    def dispose() -> None:
        for route in registered:
            if route in app.router.routes:
                app.router.routes.remove(route)
        app.openapi_schema = None

    return dispose
