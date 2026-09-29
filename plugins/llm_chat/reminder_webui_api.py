"""Password-authenticated administration of durable reminders by conversation scope."""

from __future__ import annotations

import json
from typing import Annotated
from pathlib import Path
from secrets import token_urlsafe
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from urllib.parse import urlsplit

from fastapi import Query, Depends, Request, APIRouter, HTTPException
from fastapi.routing import APIRoute
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from entari_plugin_database import get_session
from entari_plugin_webui.api.deps import require_auth
from entari_plugin_webui.core.session import Session
from entari_plugin_webui.core.security import is_local_mode

from utils.webui_theme import themed_assets
from utils.reminder_core import parse_schedule

from .models import ReminderTask
from .reminders import store
from .reminder_admin_queries import (
    _scopes,
    _scope_id,
    _sessions,
    _source_rows,
    _RequestError,
    _task_projection,
    _list_projections,
)
from .reminder_admin_inspection import _occurrences, _occurrence_detail

_API_PREFIX = "/api/llm-chat/reminders"
_PAGE_HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
_UPDATE_FIELDS = frozenset({"goal", "background", "continuation_state", "schedule", "timezone", "expires_at"})


def _failure(message: str, code: str, status: int) -> JSONResponse:
    return JSONResponse({"success": False, "code": code, "message": message}, status_code=status, headers=_PAGE_HEADERS)


class _ReminderRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def guarded(request: Request):
            try:
                return await handler(request)
            except _RequestError as exc:
                return _failure(exc.message, exc.code, exc.status)
            except RequestValidationError:
                return _failure("查询参数无效", "invalid_request", 400)
            except HTTPException as exc:
                return _failure("需要登录 WebUI 后管理提醒", "authentication_required", exc.status_code)

        return guarded


def _authenticated(session: Session | None = Depends(require_auth)) -> Session:
    if is_local_mode() or session is None:
        raise HTTPException(status_code=403, detail="需要启用 WebUI 登录后管理提醒")
    return session


def _origin(value: str) -> tuple[str, str, int]:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("invalid origin")
    return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)


async def _write_payload(request: Request) -> dict[str, object]:
    try:
        origin = request.headers.get("origin")
        if not origin or _origin(origin) != _origin(str(request.base_url)):
            raise ValueError("cross-origin request")
        if request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
            raise ValueError("cross-origin request")
    except ValueError:
        raise _RequestError("需要同源浏览器请求", "origin_rejected", 403) from None
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise _RequestError("需要 JSON 请求体")
    length = request.headers.get("content-length")
    if length and (not length.isdigit() or int(length) > 8192):
        raise _RequestError("请求体过大")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 8192:
            raise _RequestError("请求体过大")
        body.extend(chunk)

    def reject_nonfinite(_value: str) -> None:
        raise ValueError("nonfinite JSON")

    try:
        payload = json.loads(body, parse_constant=reject_nonfinite)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise _RequestError("请求体不是有效 JSON") from None
    if not isinstance(payload, dict):
        raise _RequestError("请求体必须是对象")
    return payload


def _validate_manage(payload: dict[str, object]) -> tuple[str, int, str]:
    action, scope_ref, revision = payload.get("action"), payload.get("scope_ref"), payload.get("revision")
    if (
        action not in ("update", "pause", "resume", "cancel")
        or not isinstance(scope_ref, str)
        or not scope_ref
        or len(scope_ref) > 200
        or type(revision) is not int
        or revision < 0
    ):
        raise _RequestError("操作、聊天范围或修订号无效")
    allowed = {"action", "scope_ref", "revision"} | (_UPDATE_FIELDS if action == "update" else set())
    if set(payload) - allowed or (action == "update" and not set(payload).intersection(_UPDATE_FIELDS)):
        raise _RequestError("操作字段无效")
    return scope_ref, revision, action


def _update_values(payload: dict[str, object], task: ReminderTask) -> dict[str, object]:
    values: dict[str, object] = {}
    for name, maximum in (
        ("goal", store.MAX_GOAL),
        ("background", store.MAX_BACKGROUND),
        ("continuation_state", store.MAX_CONTINUATION),
    ):
        if name in payload:
            value = payload[name]
            if not isinstance(value, str) or len(value.strip()) > maximum or (name == "goal" and not value.strip()):
                raise _RequestError(f"{name} 内容无效")
            values[name] = value.strip()
    timezone_name = payload.get("timezone", task.timezone)
    if not isinstance(timezone_name, str) or len(timezone_name) > store.MAX_TIMEZONE:
        raise _RequestError("时区无效")
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise _RequestError("时区无效") from None
    if "timezone" in payload:
        values["timezone_name"] = timezone_name
    now = datetime.now(timezone.utc)
    if "schedule" in payload:
        schedule_value = payload["schedule"]
        if not isinstance(schedule_value, str):
            raise _RequestError("时间无效")
        try:
            values["due_at"] = parse_schedule(schedule_value, timezone_name, now=now).utc
        except (ValueError, TypeError) as exc:
            raise _RequestError(str(exc)) from None
    if "expires_at" in payload:
        expiry_value = payload["expires_at"]
        if not isinstance(expiry_value, str):
            raise _RequestError("失效时间无效")
        try:
            values["expires_at"] = parse_schedule(expiry_value, timezone_name, now=now).utc
        except (ValueError, TypeError) as exc:
            raise _RequestError(str(exc)) from None
    return values


async def _manage(task_ref: str, request: Request) -> dict[str, object]:
    payload = await _write_payload(request)
    scope_ref, revision, action = _validate_manage(payload)
    async with get_session() as db:
        scope_id = await _scope_id(db, scope_ref)
        row = (await db.execute(_source_rows().where(ReminderTask.task_ref == task_ref))).one_or_none()
        if row is None:
            raise _RequestError("任务不存在", "task_not_found", 404)
        task, trusted_scope, _source_turn, _source_session = row
        if (scope_ref == "unassigned" and trusted_scope is not None) or (
            scope_ref not in ("all", "unassigned") and (trusted_scope is None or trusted_scope.id != scope_id)
        ):
            raise _RequestError("任务不属于此聊天范围", "task_not_found", 404)
        if task.revision != revision:
            raise _RequestError("任务已发生变化，请刷新", "revision_conflict", 409)
        if task.status not in {"active", "paused"}:
            raise _RequestError("已结束的任务不能修改", "task_terminal", 409)
        if action == "pause" and task.status != "active":
            raise _RequestError("只有待执行任务可以暂停", "revision_conflict", 409)
        values = _update_values(payload, task) if action == "update" else {}
        owner = {
            "account_id": task.account_id,
            "unified_user_id": task.unified_user_id,
            "platform": task.platform,
            "platform_user_id": task.platform_user_id or None,
        }
    operation_id = f"webui_{token_urlsafe(24)}"
    mutation = {
        "update": store.update_task,
        "pause": store.pause_task,
        "resume": store.resume_task,
        "cancel": store.cancel_task,
    }[action]
    try:
        await mutation(task_ref, operation_id=operation_id, expected_revision=revision, **owner, **values)
    except Exception as exc:
        # Commit acknowledgement can fail after a durable write. Never submit it again.
        try:
            confirmed = await store.get_mutation(operation_id)
        except Exception:
            confirmed = None
        if (
            confirmed is not None
            and confirmed.task_ref == task_ref
            and confirmed.action == action
            and confirmed.revision == revision + 1
        ):
            return {"success": True, "item": await _task_projection(task_ref)}
        if isinstance(exc, store.ReminderNotFound):
            raise _RequestError("任务不存在", "task_not_found", 404) from None
        if isinstance(exc, store.ReminderConflict):
            raise _RequestError("任务状态或修订号已变化，请刷新", "revision_conflict", 409) from None
        if isinstance(exc, ValueError):
            raise _RequestError(str(exc)) from None
        raise _RequestError("提交结果不明，请刷新任务列表后再决定是否操作", "mutation_unknown", 503) from None
    return {"success": True, "item": await _task_projection(task_ref)}


def create_reminder_webui_router(*, asset_dir: Path) -> APIRouter:
    router = APIRouter(
        prefix=_API_PREFIX, tags=["reminders"], dependencies=[Depends(_authenticated)], route_class=_ReminderRoute
    )

    @router.get("/page", include_in_schema=False, response_model=None)
    async def page() -> HTMLResponse | JSONResponse:
        nonce = token_urlsafe(24)
        try:
            document = (asset_dir / "index.html").read_text(encoding="utf-8")
            stylesheet, script = themed_assets(asset_dir)
        except (OSError, UnicodeError):
            return _failure("提醒页面暂不可用", "page_unavailable", 503)
        document = document.replace("<!-- REMINDER_STYLES -->", f'<style nonce="{nonce}">{stylesheet}</style>')
        document = document.replace("<!-- REMINDER_SCRIPT -->", f'<script nonce="{nonce}">{script}</script>')
        headers = dict(_PAGE_HEADERS)
        headers["Content-Security-Policy"] = (
            f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
            "connect-src 'self'; img-src 'self' blob:; base-uri 'none'; object-src 'none'; "
            "frame-ancestors 'self'; form-action 'none'"
        )
        return HTMLResponse(document, headers=headers)

    @router.get("/scopes", response_model=None)
    async def scopes(
        limit: Annotated[int, Query(ge=1, le=100)] = 40, offset: Annotated[int, Query(ge=0, le=9223372036854775807)] = 0
    ) -> dict[str, object]:
        items, has_more = await _scopes(limit, offset)
        return {"success": True, "items": items, "has_more": has_more}

    @router.get("/sessions", response_model=None)
    async def sessions(
        scope_ref: Annotated[str, Query(max_length=200)],
        limit: Annotated[int, Query(ge=1, le=100)] = 40,
        offset: Annotated[int, Query(ge=0, le=9223372036854775807)] = 0,
    ) -> dict[str, object]:
        items, has_more, total_count = await _sessions(scope_ref, limit, offset)
        return {"success": True, "items": items, "has_more": has_more, "total_count": total_count}

    @router.get("/tasks", response_model=None)
    async def tasks(
        scope_ref: Annotated[str, Query(max_length=200)] = "all",
        session_ref: Annotated[str, Query(max_length=200)] = "all",
        status: Annotated[str, Query(max_length=30)] = "all",
        q: Annotated[str, Query(max_length=120)] = "",
        limit: Annotated[int, Query(ge=1, le=100)] = 40,
        offset: Annotated[int, Query(ge=0, le=9223372036854775807)] = 0,
    ) -> dict[str, object]:
        items, has_more, total_count = await _list_projections(scope_ref, session_ref, status, q, limit, offset)
        return {"success": True, "items": items, "has_more": has_more, "total_count": total_count}

    @router.get("/tasks/{task_ref}", response_model=None)
    async def task_detail(task_ref: str) -> dict[str, object]:
        return {"success": True, "item": await _task_projection(task_ref, include_source=True)}

    @router.get("/tasks/{task_ref}/occurrences", response_model=None)
    async def occurrences(
        task_ref: str,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0, le=9223372036854775807)] = 0,
    ) -> dict[str, object]:
        items, has_more, total_count = await _occurrences(task_ref, limit, offset)
        return {"success": True, "items": items, "has_more": has_more, "total_count": total_count}

    @router.get("/tasks/{task_ref}/occurrences/{sequence}", response_model=None)
    async def occurrence_detail(task_ref: str, sequence: int) -> dict[str, object]:
        return {"success": True, "item": await _occurrence_detail(task_ref, sequence)}

    @router.post("/tasks/{task_ref}/manage", response_model=None)
    async def manage(task_ref: str, request: Request) -> dict[str, object] | JSONResponse:
        try:
            return await _manage(task_ref, request)
        except _RequestError as exc:
            return _failure(exc.message, exc.code, exc.status)

    return router
