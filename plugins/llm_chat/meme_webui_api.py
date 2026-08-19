"""FastAPI routes for the meme administration WebUI extension."""

from __future__ import annotations

import json
from typing import Annotated
from pathlib import Path
from urllib.parse import quote
from collections.abc import Mapping, Callable

from fastapi import File, Form, Query, Depends, Request, APIRouter, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from .meme_admin import MemeAdminError, MemeAdminService
from .meme_catalog import MemeCatalogItem, MemeCatalogSort, MemeCatalogFilter
from .core.image_source import IMAGE_FETCH_MAX_BYTES

_API_PREFIX = "/api/llm-chat/memes"
_ASSET_FILES = {
    "app.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
}
_PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'self'"
    ),
}
AuthDependency = Callable[..., object]


def _error_response(exc: MemeAdminError) -> JSONResponse:
    return JSONResponse(
        {"success": False, "code": exc.code, "message": str(exc)},
        status_code=exc.status,
    )


def _serialize_item(item: MemeCatalogItem) -> dict[str, object]:
    payload = item.to_dict()
    payload["image_url"] = (
        f"{_API_PREFIX}/files/{quote(item.file_name, safe='')}?v={item.version}" if item.status != "missing" else None
    )
    return payload


async def _read_tag_payload(request: Request) -> str:
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > 8192:
        raise MemeAdminError("Request body is too large", code="request_too_large", status=413)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 8192:
            raise MemeAdminError("Request body is too large", code="request_too_large", status=413)
        body.extend(chunk)
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise MemeAdminError("Request body must be valid JSON", code="invalid_json", status=400) from None
    if not isinstance(payload, Mapping) or not isinstance(payload.get("tags"), str):
        raise MemeAdminError("A string tags field is required", code="invalid_tags", status=400)
    return payload["tags"]


def create_meme_admin_router(
    service: MemeAdminService,
    *,
    asset_dir: Path,
    auth_dependency: AuthDependency | None = None,
) -> APIRouter:
    """Build an injectable, authenticated administration router."""

    dependencies = [Depends(auth_dependency)] if auth_dependency is not None else []
    router = APIRouter(prefix=_API_PREFIX, tags=["meme-admin"], dependencies=dependencies)

    @router.get("/page", include_in_schema=False, response_model=None)
    async def page() -> FileResponse | JSONResponse:
        path = asset_dir / "index.html"
        if not path.is_file():
            return JSONResponse(
                {"success": False, "code": "page_unavailable", "message": "Meme manager page is unavailable"},
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

    @router.get("/files/{file_name}", include_in_schema=False, response_model=None)
    async def image(file_name: str) -> FileResponse | JSONResponse:
        path = service.resolve_file(file_name)
        if path is None or not path.is_file():
            return JSONResponse(
                {"success": False, "code": "image_not_found", "message": "Meme image was not found"},
                status_code=404,
            )
        return FileResponse(
            path,
            filename=file_name,
            content_disposition_type="inline",
            headers={"Cache-Control": "private, max-age=60", "X-Content-Type-Options": "nosniff"},
        )

    @router.get("", response_model=None)
    async def list_memes(
        q: Annotated[str, Query(max_length=200)] = "",
        status: MemeCatalogFilter = "all",
        sort: MemeCatalogSort = "newest",
        page_number: Annotated[int, Query(alias="page", ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 24,
    ) -> dict[str, object] | JSONResponse:
        try:
            result = await service.list_memes(
                query=q,
                status=status,
                sort=sort,
                page=page_number,
                page_size=page_size,
            )
        except MemeAdminError as exc:
            return _error_response(exc)
        return {
            "success": True,
            "items": [_serialize_item(item) for item in result.items],
            "total": result.total,
            "page": result.page,
            "page_size": result.page_size,
            "pages": result.pages,
            "stats": result.stats,
            "max_upload_bytes": IMAGE_FETCH_MAX_BYTES,
            "accepted_formats": ["JPEG", "PNG", "WebP", "GIF"],
        }

    @router.post("", status_code=201, response_model=None)
    async def upload_meme(
        file: Annotated[UploadFile, File()],
        tags: Annotated[str, Form(max_length=4000)] = "",
        auto_tag: Annotated[bool, Form()] = True,
    ) -> dict[str, object] | JSONResponse:
        try:
            data = await file.read(IMAGE_FETCH_MAX_BYTES + 1)
        finally:
            await file.close()
        try:
            outcome = await service.upload(data, tags=tags, auto_tag=auto_tag)
        except MemeAdminError as exc:
            return _error_response(exc)
        return {
            "success": True,
            "status": outcome.status,
            "item": _serialize_item(outcome.item),
        }

    @router.patch("/{file_name}", response_model=None)
    async def update_tags(file_name: str, request: Request) -> dict[str, object] | JSONResponse:
        try:
            tags = await _read_tag_payload(request)
            item = await service.update_tags(file_name, tags)
        except MemeAdminError as exc:
            return _error_response(exc)
        return {"success": True, "item": _serialize_item(item)}

    @router.post("/{file_name}/retag", response_model=None)
    async def retag_meme(file_name: str) -> dict[str, object] | JSONResponse:
        try:
            item = await service.retag(file_name)
        except MemeAdminError as exc:
            return _error_response(exc)
        return {"success": True, "item": _serialize_item(item)}

    @router.delete("/{file_name}", response_model=None)
    async def delete_meme(file_name: str) -> dict[str, object] | JSONResponse:
        try:
            result = await service.delete(file_name)
        except MemeAdminError as exc:
            return _error_response(exc)
        return {
            "success": True,
            "file_deleted": result.file_deleted,
            "index_deleted": result.index_deleted,
        }

    return router
