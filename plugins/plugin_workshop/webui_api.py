"""Bounded, authenticated read-only views of immutable workshop candidates."""

from __future__ import annotations

from pathlib import Path
from secrets import token_urlsafe
from collections.abc import Callable

from fastapi import Depends, Request, APIRouter, HTTPException
from fastapi.routing import APIRoute
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.exceptions import RequestValidationError

from utils.webui_theme import themed_assets
from utils.plugin_workshop_core.codec import version_payload
from utils.plugin_workshop_core.models import Actor, WorkshopAPI, WorkshopError

API_PREFIX = "/api/plugin-workshop"
_ASSETS = {"app.css": "text/css; charset=utf-8", "app.js": "text/javascript; charset=utf-8"}
_HEADERS = {
    "Cache-Control": "private, no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'self'; form-action 'none'"
    ),
}
_MAX_RESPONSE = 8 * 1024 * 1024


def _failure(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse(
        {"success": False, "code": code, "message": message[:2000]}, status_code=status, headers=_HEADERS
    )


class WorkshopRoute(APIRoute):
    """Keep dependency, parsing, and backend failures in one safe envelope."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def guarded(request: Request):
            try:
                return await handler(request)
            except WorkshopError as exc:
                return _failure(exc.code, str(exc), exc.status)
            except RequestValidationError:
                return _failure("invalid_request", "Request parameters are invalid", 422)
            except HTTPException as exc:
                return _failure("http_error", "Request was rejected", exc.status_code)
            except Exception:
                # Exception reprs can contain paths, credentials, or submitted source.
                return _failure("internal_error", "Workshop request failed; inspect the server privately", 500)

        return guarded


def authenticated_webui_actor(request: Request) -> Actor:
    """Require a password-authenticated session, never a passwordless cookie."""
    from entari_plugin_webui.api.deps import get_session_store
    from entari_plugin_webui.core.security import is_local_mode

    store = get_session_store()
    session = store.get(request.cookies.get("webui_sid"))
    if is_local_mode() or session is None:
        raise WorkshopError(
            "Sign in with WebUI password authentication; passwordless local mode is not administrator access.",
            code="authentication_required",
            status=401,
        )
    store.refresh_if_needed(session)
    return Actor(key="authenticated-webui", is_admin=True)


def _reply(*, item: object = None, items: object = None) -> JSONResponse:
    payload = {"success": True, "items": items} if items is not None else {"success": True, "item": item}
    response = JSONResponse(payload, headers=_HEADERS)
    if len(response.body) > _MAX_RESPONSE:
        raise WorkshopError("Response is too large; request a smaller page", code="response_too_large", status=413)
    return response


def _page_number(request: Request, key: str, default: int, maximum: int, *, minimum: int = 0) -> int:
    raw = request.query_params.get(key, str(default))
    if not raw.isascii() or not raw.isdigit() or len(raw) > 9:
        raise WorkshopError(f"{key} must be an integer")
    number = int(raw)
    if not minimum <= number <= maximum:
        raise WorkshopError(f"{key} is outside the allowed range")
    return number


def create_workshop_router(
    service: WorkshopAPI,
    *,
    asset_dir: Path,
    auth_dependency: Callable[..., object] = authenticated_webui_actor,
) -> APIRouter:
    """The injection seam accepts only trusted server-provided administrator Actors."""

    async def admin_actor(actor: object = Depends(auth_dependency)) -> Actor:
        if not isinstance(actor, Actor) or not actor.is_admin:
            raise WorkshopError("Administrator authentication is required", code="authentication_required", status=401)
        return actor

    router = APIRouter(
        prefix=API_PREFIX, route_class=WorkshopRoute, tags=["plugin-workshop"], dependencies=[Depends(admin_actor)]
    )

    @router.get("/page", response_model=None, include_in_schema=False)
    async def page():
        nonce = token_urlsafe(24)
        try:
            document = (asset_dir / "index.html").read_text(encoding="utf-8")
            stylesheet, script = themed_assets(asset_dir)
        except OSError:
            raise WorkshopError("Workshop page is unavailable", code="page_unavailable", status=503) from None
        # Native WebUI frames have an opaque origin: embed trusted assets and use its API bridge.
        document = document.replace("<!-- WORKSHOP_STYLES -->", f'<style nonce="{nonce}">{stylesheet}</style>')
        document = document.replace("<!-- WORKSHOP_SCRIPT -->", f'<script nonce="{nonce}">{script}</script>')
        headers = dict(_HEADERS)
        headers["Content-Security-Policy"] = (
            f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
            "connect-src 'self'; img-src 'self'; base-uri 'none'; object-src 'none'; "
            "frame-ancestors 'self'; form-action 'none'"
        )
        return HTMLResponse(document, headers=headers)

    @router.get("/assets/{asset_name}", response_model=None, include_in_schema=False)
    async def asset(asset_name: str):
        media = _ASSETS.get(asset_name)
        if media is None or not (asset_dir / asset_name).is_file():
            raise WorkshopError("Asset not found", code="not_found", status=404)
        return FileResponse(asset_dir / asset_name, media_type=media, headers=_HEADERS)

    @router.get("/status", response_model=None)
    async def status():
        return _reply(item=await service.get_status())

    @router.get("/projects", response_model=None)
    async def projects(request: Request, actor: Actor = Depends(admin_actor)):
        return _reply(
            items=await service.list_projects(
                actor,
                limit=_page_number(request, "limit", 50, 100, minimum=1),
                offset=_page_number(request, "offset", 0, 1000000),
            )
        )

    @router.get("/projects/{name}/versions", response_model=None)
    async def versions(name: str, request: Request, actor: Actor = Depends(admin_actor)):
        limit = _page_number(request, "limit", 10, 20, minimum=1)
        offset = _page_number(request, "offset", 0, 256)
        records = await service.list_versions(name, actor)
        return _reply(items=[version_payload(record) for record in records[offset : offset + limit]])

    @router.get("/projects/{name}/versions/{version}", response_model=None)
    async def detail(name: str, version: int, actor: Actor = Depends(admin_actor)):
        return _reply(item=version_payload(await service.detail(name, version, actor)))

    @router.get("/projects/{name}/versions/{version}/source", response_model=None)
    async def source(name: str, version: int, actor: Actor = Depends(admin_actor)):
        return _reply(item=await service.source(name, version, actor))

    return router
