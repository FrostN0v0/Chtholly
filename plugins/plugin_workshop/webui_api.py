"""Bounded, authenticated administration of immutable workshop candidates."""

from __future__ import annotations

import re
import json
from pathlib import Path
from secrets import token_urlsafe
from urllib.parse import urlsplit
from collections.abc import Mapping, Callable

from fastapi import Depends, Request, APIRouter, HTTPException
from fastapi.routing import APIRoute
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.exceptions import RequestValidationError

from utils.webui_theme import themed_assets
from utils.plugin_workshop_core.codec import state_payload, version_payload
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


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Invalid origin")
    return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)


def _same_origin(request: Request) -> None:
    supplied = request.headers.get("origin")
    try:
        if not supplied or _origin(supplied) != _origin(str(request.url)):
            raise ValueError("Cross-origin request")
        parsed = urlsplit(supplied)
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError("Invalid origin")
        if request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
            raise ValueError("Cross-origin request")
    except ValueError:
        raise WorkshopError("A same-origin browser request is required", code="origin_rejected", status=403) from None


def _reject_nonfinite(value: str) -> None:
    raise ValueError("Non-finite JSON is not accepted")


async def _body(request: Request, *, maximum: int = 8192) -> dict[str, object]:
    _same_origin(request)
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise WorkshopError("Content-Type must be application/json", code="invalid_content_type", status=415)
    length = request.headers.get("content-length")
    if length and (not length.isdigit() or int(length) > maximum):
        raise WorkshopError("Request body is too large or its length is invalid", code="request_too_large", status=413)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > maximum:
            raise WorkshopError("Request body is too large", code="request_too_large", status=413)
        body.extend(chunk)
    try:
        payload = json.loads(body, parse_constant=_reject_nonfinite)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise WorkshopError("Request body must be valid finite JSON", code="invalid_json") from None
    if not isinstance(payload, dict):
        raise WorkshopError("Request body must be a JSON object", code="invalid_json")
    return payload


def _hash(payload: Mapping[str, object], *, approval: bool = False) -> str:
    keys = {"source_hash", "acknowledge_native"} if approval else {"source_hash"}
    if set(payload) != keys:
        raise WorkshopError("Only the required hash and acknowledgement fields are accepted")
    source_hash = payload.get("source_hash")
    if not isinstance(source_hash, str) or re.fullmatch(r"[0-9a-f]{64}", source_hash) is None:
        raise WorkshopError("source_hash must be the exact displayed SHA-256 digest")
    if approval and payload.get("acknowledge_native") is not True:
        raise WorkshopError(
            "Explicit acknowledgement of full native process privileges is required", code="native_ack_required"
        )
    return source_hash


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

    @router.post("/projects/{name}/versions", response_model=None)
    async def submit(name: str, request: Request, actor: Actor = Depends(admin_actor)):
        payload = await _body(request, maximum=2 * 1024 * 1024)
        if (
            set(payload) != {"files", "manifest"}
            or not isinstance(payload["files"], dict)
            or not isinstance(payload["manifest"], dict)
        ):
            raise WorkshopError("Submission requires only files and manifest objects")
        files = payload["files"]
        if not all(isinstance(path, str) and isinstance(text, str) for path, text in files.items()):
            raise WorkshopError("Files must map relative paths to exact UTF-8 source text")
        return _reply(item=version_payload(await service.submit(name, files, payload["manifest"], actor)))

    @router.post("/projects/{name}/versions/{version}/validate", response_model=None)
    async def validate(name: str, version: int, request: Request, actor: Actor = Depends(admin_actor)):
        if await _body(request):
            raise WorkshopError("Revalidation accepts an empty JSON object only")
        return _reply(item=version_payload(await service.validate(name, version, actor)))

    @router.post("/projects/{name}/versions/{version}/approve", response_model=None)
    async def approve(name: str, version: int, request: Request, actor: Actor = Depends(admin_actor)):
        digest = _hash(await _body(request), approval=True)
        return _reply(item=version_payload(await service.approve(name, version, digest, actor)))

    @router.post("/projects/{name}/versions/{version}/activate", response_model=None)
    async def activate(name: str, version: int, request: Request, actor: Actor = Depends(admin_actor)):
        digest = _hash(await _body(request))
        return _reply(item=state_payload(await service.activate(name, version, digest, actor)))

    @router.post("/projects/{name}/versions/{version}/rollback", response_model=None)
    async def rollback(name: str, version: int, request: Request, actor: Actor = Depends(admin_actor)):
        digest = _hash(await _body(request))
        return _reply(item=state_payload(await service.rollback(name, version, digest, actor)))

    @router.post("/projects/{name}/disable", response_model=None)
    async def disable(name: str, request: Request, actor: Actor = Depends(admin_actor)):
        if await _body(request):
            raise WorkshopError("Disable accepts an empty JSON object only")
        return _reply(item=state_payload(await service.disable(name, actor)))

    return router
