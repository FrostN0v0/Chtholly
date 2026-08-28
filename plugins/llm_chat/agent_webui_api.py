"""Authenticated FastAPI routes for AgentEvent session administration."""

from __future__ import annotations

import json
from typing import Annotated
from pathlib import Path
from collections.abc import Mapping, Callable

from fastapi import Query, Depends, Request, APIRouter
from fastapi.responses import FileResponse, JSONResponse

from .agent_admin import AgentAdminError, AgentAdminService

_API_PREFIX = "/api/llm-chat/sessions"
_ASSET_FILES = {
    "app.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
}
_PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'self'"
    ),
}
AuthDependency = Callable[..., object]


def _error_response(exc: AgentAdminError) -> JSONResponse:
    return JSONResponse(
        {"success": False, "code": exc.code, "message": str(exc)},
        status_code=exc.status,
    )


async def _read_json(request: Request, *, maximum: int = 8192) -> Mapping[str, object]:
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > maximum:
        raise AgentAdminError("Request body is too large", code="request_too_large", status=413)
    body = await request.body()
    if len(body) > maximum:
        raise AgentAdminError("Request body is too large", code="request_too_large", status=413)
    try:
        payload = json.loads(body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise AgentAdminError("Request body must be valid JSON", code="invalid_json") from None
    if not isinstance(payload, Mapping):
        raise AgentAdminError("Request body must be a JSON object", code="invalid_json")
    return payload


def create_agent_sessions_router(
    service: AgentAdminService,
    *,
    asset_dir: Path,
    auth_dependency: AuthDependency | None = None,
) -> APIRouter:
    dependencies = [Depends(auth_dependency)] if auth_dependency is not None else []
    router = APIRouter(prefix=_API_PREFIX, tags=["agent-sessions"], dependencies=dependencies)

    @router.get("/page", include_in_schema=False, response_model=None)
    async def page() -> FileResponse | JSONResponse:
        path = asset_dir / "index.html"
        if not path.is_file():
            return JSONResponse(
                {"success": False, "code": "page_unavailable", "message": "Agent sessions page is unavailable"},
                status_code=503,
            )
        return FileResponse(path, media_type="text/html; charset=utf-8", headers=_PAGE_HEADERS)

    @router.get("/assets/{asset_name}", include_in_schema=False, response_model=None)
    async def asset(asset_name: str) -> FileResponse | JSONResponse:
        media_type = _ASSET_FILES.get(asset_name)
        path = asset_dir / asset_name
        if media_type is None or not path.is_file():
            return JSONResponse(
                {"success": False, "code": "asset_not_found", "message": "Asset was not found"},
                status_code=404,
            )
        return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-cache"})

    @router.get("/scopes", response_model=None)
    async def scopes(limit: Annotated[int, Query(ge=1, le=500)] = 100) -> dict[str, object]:
        return {"success": True, "items": await service.list_scopes(limit)}

    @router.get("/scopes/{scope_ref}/sessions", response_model=None)
    async def sessions(
        scope_ref: str,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> dict[str, object] | JSONResponse:
        try:
            items = await service.list_sessions(scope_ref, limit)
        except AgentAdminError as exc:
            return _error_response(exc)
        return {"success": True, "items": items}

    @router.get("/sessions/{session_ref}", response_model=None)
    async def session_detail(session_ref: str) -> dict[str, object] | JSONResponse:
        try:
            item = await service.session_detail(session_ref)
        except AgentAdminError as exc:
            return _error_response(exc)
        return {"success": True, "item": item}

    @router.get("/sessions/{session_ref}/turns", response_model=None)
    async def turns(
        session_ref: str,
        limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    ) -> dict[str, object] | JSONResponse:
        try:
            items = await service.list_turns(session_ref, limit)
        except AgentAdminError as exc:
            return _error_response(exc)
        return {"success": True, "items": items}

    @router.get("/turns/{turn_ref}/events", response_model=None)
    async def events(turn_ref: str) -> dict[str, object] | JSONResponse:
        try:
            items = await service.list_events(turn_ref)
        except AgentAdminError as exc:
            return _error_response(exc)
        return {"success": True, "items": items}

    @router.get("/turns/{turn_ref}/context", response_model=None)
    async def context_inspector(turn_ref: str) -> dict[str, object] | JSONResponse:
        try:
            item = await service.context_inspector(turn_ref)
        except AgentAdminError as exc:
            return _error_response(exc)
        return {"success": True, "item": item}

    @router.get("/events/{event_ref}/payload", response_model=None)
    async def event_payload(
        event_ref: str,
        path: Annotated[str, Query(max_length=300)] = "",
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=256, le=100000)] = 16000,
    ) -> dict[str, object] | JSONResponse:
        try:
            item = await service.read_event_payload(event_ref, path=path, offset=offset, limit=limit)
        except AgentAdminError as exc:
            return _error_response(exc)
        return {"success": True, "item": item}

    @router.post("/scopes/{scope_ref}/sessions/{session_ref}/rollover", response_model=None)
    async def rollover(scope_ref: str, session_ref: str, request: Request) -> dict[str, object] | JSONResponse:
        try:
            payload = await _read_json(request)
            carry_handoff = payload.get("carry_handoff", True)
            if type(carry_handoff) is not bool:
                raise AgentAdminError("carry_handoff must be a boolean")
            item = await service.rollover(scope_ref, session_ref, carry_handoff=carry_handoff)
        except AgentAdminError as exc:
            return _error_response(exc)
        return {"success": True, "item": item}

    @router.post("/scopes/{scope_ref}/hard-reset", response_model=None)
    async def hard_reset(scope_ref: str, request: Request) -> dict[str, object] | JSONResponse:
        try:
            payload = await _read_json(request)
            if payload.get("confirmation") != "CONFIRM":
                raise AgentAdminError("Exact confirmation token CONFIRM is required", code="confirmation_required")
            item = await service.hard_reset(scope_ref)
        except AgentAdminError as exc:
            return _error_response(exc)
        return {"success": True, "item": item}

    @router.post("/scopes/{scope_ref}/events/{event_ref}/pin", response_model=None)
    async def pin(scope_ref: str, event_ref: str, request: Request) -> dict[str, object] | JSONResponse:
        try:
            payload = await _read_json(request)
            label = payload.get("label")
            if not isinstance(label, str) or not label.strip():
                raise AgentAdminError("A non-empty label is required")
            item = await service.pin(scope_ref, event_ref, label[:200])
        except AgentAdminError as exc:
            return _error_response(exc)
        return {"success": True, "item": item}

    @router.delete("/scopes/{scope_ref}/events/{event_ref}/pin", response_model=None)
    async def unpin(scope_ref: str, event_ref: str) -> dict[str, object] | JSONResponse:
        try:
            item = await service.unpin(scope_ref, event_ref)
        except AgentAdminError as exc:
            return _error_response(exc)
        return {"success": True, "item": item}

    return router
