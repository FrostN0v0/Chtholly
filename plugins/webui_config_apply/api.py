"""Protect native WebUI mutations without replacing its configuration API contract."""

from __future__ import annotations

import re
from urllib.parse import urlsplit
from collections.abc import Callable

from fastapi import Depends, FastAPI, Request, APIRouter, HTTPException
from starlette.types import Send, Scope, ASGIApp, Receive
from fastapi.responses import JSONResponse
from starlette.middleware import Middleware
from arclet.entari.config.file import EntariConfig
from entari_plugin_webui.api.deps import require_auth
from entari_plugin_webui.services import config_service
from entari_plugin_webui.core.error import AppError

from utils.webui_config_core import ConfigValidationError

from .saving import SaveError, ConfigSaver, requires_managed_restart
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


def _safe_error(exc: Exception) -> AppError:
    if isinstance(exc, ConfigValidationError):
        status = {
            "source_changed": 409,
            "stale_runtime": 409,
            "config_rollback_failed": 503,
            "model_state_failed": 503,
        }.get(exc.code, 400)
        return AppError(message=str(exc), code=exc.code, status=status)
    if isinstance(exc, SaveError):
        return AppError(message=str(exc), code=exc.code, status=exc.status)
    if isinstance(exc, AppError):
        return exc
    if isinstance(exc, OSError):
        return AppError(
            message="Configuration files are unavailable or not writable", code="config_io_failed", status=503
        )
    # Parser failures may include decrypted configuration and must not reach the generic exception logger.
    return AppError(
        message="Configuration could not be prepared safely; reload the page and review its structure",
        code="config_save_failed",
        status=400,
    )


def _error(exc: Exception) -> JSONResponse:
    error = _safe_error(exc)
    return JSONResponse(
        {"success": False, "code": error.code, "message": error.message},
        status_code=error.status,
        headers={"Cache-Control": "no-store"},
    )


class ConfigMutationGuard:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        guarded = path.startswith(("/api/plugins", "/api/config/", "/api/market"))
        if scope["type"] == "http" and scope["method"] not in {"GET", "HEAD", "OPTIONS"} and guarded:
            try:
                require_same_origin(Request(scope))
                action = re.fullmatch(r"/api/plugins/([^/]+)/(toggle|reload|unload|uninstall)", path)
                if action:
                    plugin_id = action.group(1)
                    keys = config_service.get_plugin_config_path(plugin_id) or [plugin_id]
                    if any(key.lstrip("~?").startswith("workshop_") for key in keys):
                        raise SaveError(
                            "Workshop plugins must be managed through workshop tools",
                            code="workshop_managed",
                            status=403,
                        )
                    if requires_managed_restart(keys[0]):
                        raise SaveError(
                            "This core plugin requires a managed configuration restart",
                            code="managed_restart_required",
                            status=409,
                        )
            except HTTPException as exc:
                response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
                await response(scope, receive, send)
                return
            except SaveError as exc:
                await _error(exc)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def install_api(app: FastAPI, running_sha256: str | None) -> Callable[[], None]:
    saver = ConfigSaver(running_sha256, restart_available=lambda: read_status()[0])
    router = APIRouter(dependencies=[Depends(require_auth), Depends(require_same_origin)])
    current = config_service._update_section
    original = getattr(current, "_chtholly_config_apply_original", current)

    async def protected_update(section, payload, **options):
        try:
            return await saver.save_section(section, payload, **options)
        except AppError:
            raise
        except Exception as exc:
            raise _safe_error(exc) from None

    setattr(protected_update, "_chtholly_config_apply_original", original)
    config_service._update_section = protected_update
    middleware = Middleware(ConfigMutationGuard)
    app.user_middleware.insert(0, middleware)
    app.middleware_stack = None

    @router.get("/api/config-apply/status")
    async def status():
        try:
            return JSONResponse(
                status_payload(
                    EntariConfig.instance.path,
                    saver.running_sha256,
                    application_mode=saver.application_mode,
                    last_application=saver.last_application,
                ),
                headers={"Cache-Control": "no-store"},
            )
        except Exception as exc:
            return _error(exc)

    @router.post("/api/config-apply/restart")
    async def restart():
        try:
            async with saver.lock:
                request_id = request_restart()
            return JSONResponse({"success": True, "request_id": request_id}, headers={"Cache-Control": "no-store"})
        except Exception as exc:
            return _error(exc)

    route_start = len(app.router.routes)
    app.include_router(router)
    registered = tuple(app.router.routes[route_start:])
    app.openapi_schema = None

    def dispose() -> None:
        if config_service._update_section is protected_update:
            config_service._update_section = original
        if middleware in app.user_middleware:
            app.user_middleware.remove(middleware)
            app.middleware_stack = None
        for route in registered:
            if route in app.router.routes:
                app.router.routes.remove(route)
        app.openapi_schema = None

    return dispose
