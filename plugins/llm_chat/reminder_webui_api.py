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
from sqlalchemy import and_, func, select
from fastapi.routing import APIRoute
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from entari_plugin_database import get_session
from entari_plugin_webui.api.deps import require_auth
from entari_plugin_webui.core.session import Session
from entari_plugin_webui.core.security import is_local_mode

from utils.webui_theme import themed_assets
from utils.reminder_core import parse_schedule

from .models import ChatScope, ReminderTask, ReminderOccurrence
from .reminders import store

_API_PREFIX = "/api/llm-chat/reminders"
_PAGE_HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
_STATUSES = frozenset({"active", "paused", "completed", "cancelled"})
_OCCURRENCE_STATES = frozenset(
    {
        "claimed",
        "model_started",
        "dm_started",
        "transport_started",
        "confirmed",
        "completed",
        "paused",
        "cancelled",
        "expired_recovered",
        "unknown",
    }
)
_UPDATE_FIELDS = frozenset({"goal", "background", "continuation_state", "schedule", "timezone", "expires_at"})


class _RequestError(Exception):
    def __init__(self, message: str, code: str = "invalid_request", status: int = 400) -> None:
        self.message, self.code, self.status = message, code, status


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


def _utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


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


def _project(task: ReminderTask, occurrence: ReminderOccurrence | None, scope_ref: str | None) -> dict[str, object]:
    # Never serialize ORM entities: lease tokens, receipt IDs and source/audit refs are private.
    return {
        "task_ref": task.task_ref,
        "scope_ref": scope_ref,
        "revision": task.revision,
        "owner_user_id": task.unified_user_id,
        "platform_user_id": task.platform_user_id,
        "goal": task.goal[: store.MAX_GOAL],
        "background": task.background[: store.MAX_BACKGROUND],
        "continuation_state": task.continuation_state[: store.MAX_CONTINUATION],
        "mode": task.mode if task.mode in {"once", "adaptive"} else "unknown",
        "origin": task.origin if task.origin in {"user", "autonomous"} else "unknown",
        "delivery": task.delivery_target_type if task.delivery_target_type in {"channel", "dm"} else "unknown",
        "status": task.status if task.status in _STATUSES else "unknown",
        "due_at": _utc(task.due_at),
        "timezone": task.timezone[: store.MAX_TIMEZONE],
        "expires_at": _utc(task.expires_at),
        "created_at": _utc(task.created_at),
        "updated_at": _utc(task.updated_at),
        "paused_reason": task.paused_reason[: store.MAX_REASON],
        "occurrence": None
        if occurrence is None
        else {
            "state": occurrence.state if occurrence.state in _OCCURRENCE_STATES else "unknown",
            "scheduled_at": _utc(occurrence.scheduled_at),
            "started_at": _utc(occurrence.started_at),
            "finished_at": _utc(occurrence.finished_at),
            "confirmed_count": max(0, occurrence.confirmed_count),
            "transport_started": occurrence.transport_started,
        },
    }


async def _scope_id(db, scope_ref: str) -> int | None:
    if scope_ref == "all":
        return None
    result = await db.scalar(select(ChatScope.id).where(ChatScope.scope_ref == scope_ref))
    if result is None:
        raise _RequestError("聊天范围不存在", "scope_not_found", 404)
    return result


async def _latest_occurrences(db, task_refs: list[str]) -> dict[str, ReminderOccurrence]:
    if not task_refs:
        return {}
    latest = (
        select(ReminderOccurrence.task_ref, func.max(ReminderOccurrence.sequence).label("sequence"))
        .where(ReminderOccurrence.task_ref.in_(task_refs))
        .group_by(ReminderOccurrence.task_ref)
        .subquery()
    )
    rows = await db.execute(
        select(ReminderOccurrence).join(
            latest,
            and_(
                ReminderOccurrence.task_ref == latest.c.task_ref,
                ReminderOccurrence.sequence == latest.c.sequence,
            ),
        )
    )
    return {row.task_ref: row for row in rows.scalars()}


async def _list_projections(scope_ref: str, limit: int, offset: int) -> tuple[list[dict[str, object]], bool]:
    async with get_session() as db:
        scope_id = await _scope_id(db, scope_ref)
        statement = select(ReminderTask, ChatScope.scope_ref).outerjoin(
            ChatScope, ReminderTask.source_scope_id == ChatScope.id
        )
        if scope_id is not None:
            statement = statement.where(ReminderTask.source_scope_id == scope_id)
        rows = (
            await db.execute(
                statement.order_by(ReminderTask.updated_at.desc(), ReminderTask.id.desc())
                .offset(offset)
                .limit(limit + 1)
            )
        ).all()
        has_more = len(rows) > limit
        rows = rows[:limit]
        occurrences = await _latest_occurrences(db, [task.task_ref for task, _ in rows])
        return [_project(task, occurrences.get(task.task_ref), ref) for task, ref in rows], has_more


async def _scopes(limit: int, offset: int) -> tuple[list[dict[str, object]], bool]:
    async with get_session() as db:
        counts = (
            select(
                ReminderTask.source_scope_id.label("scope_id"),
                func.count(ReminderTask.id).label("task_count"),
                func.count(ReminderTask.id).filter(ReminderTask.status == "active").label("active_count"),
            )
            .group_by(ReminderTask.source_scope_id)
            .subquery()
        )
        rows = (
            await db.execute(
                select(ChatScope, counts.c.task_count, counts.c.active_count)
                .join(counts, ChatScope.id == counts.c.scope_id)
                .order_by(ChatScope.updated_at.desc(), ChatScope.id.desc())
                .offset(offset)
                .limit(limit + 1)
            )
        ).all()
        return [
            {
                "scope_ref": scope.scope_ref,
                "channel_name": scope.display_name or scope.channel_id,
                "platform": scope.platform,
                "account_id": scope.account_id,
                "channel_id": scope.channel_id,
                "task_count": count or 0,
                "active_count": active or 0,
            }
            for scope, count, active in rows[:limit]
        ], len(rows) > limit


async def _task_projection(task_ref: str) -> dict[str, object]:
    async with get_session() as db:
        row = (
            await db.execute(
                select(ReminderTask, ChatScope.scope_ref)
                .outerjoin(ChatScope, ReminderTask.source_scope_id == ChatScope.id)
                .where(ReminderTask.task_ref == task_ref)
            )
        ).one_or_none()
        if row is None:
            raise _RequestError("任务不存在", "task_not_found", 404)
        task, ref = row
        occurrences = await _latest_occurrences(db, [task_ref])
        return _project(task, occurrences.get(task_ref), ref)


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
        try:
            values["due_at"] = parse_schedule(payload["schedule"], timezone_name, now=now).utc
        except (ValueError, TypeError) as exc:
            raise _RequestError(str(exc)) from None
    if "expires_at" in payload:
        try:
            values["expires_at"] = parse_schedule(payload["expires_at"], timezone_name, now=now).utc
        except (ValueError, TypeError) as exc:
            raise _RequestError(str(exc)) from None
    return values


async def _manage(task_ref: str, request: Request) -> dict[str, object]:
    payload = await _write_payload(request)
    scope_ref, revision, action = _validate_manage(payload)
    async with get_session() as db:
        scope_id = await _scope_id(db, scope_ref)
        task = await db.scalar(select(ReminderTask).where(ReminderTask.task_ref == task_ref))
        if task is None or (scope_ref != "all" and task.source_scope_id != scope_id):
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
        limit: Annotated[int, Query(ge=1, le=100)] = 40, offset: Annotated[int, Query(ge=0, le=10000)] = 0
    ) -> dict[str, object]:
        items, has_more = await _scopes(limit, offset)
        return {"success": True, "items": items, "has_more": has_more}

    @router.get("/tasks", response_model=None)
    async def tasks(
        scope_ref: Annotated[str, Query(max_length=200)] = "all",
        limit: Annotated[int, Query(ge=1, le=100)] = 40,
        offset: Annotated[int, Query(ge=0, le=10000)] = 0,
    ) -> dict[str, object] | JSONResponse:
        try:
            items, has_more = await _list_projections(scope_ref, limit, offset)
        except _RequestError as exc:
            return _failure(exc.message, exc.code, exc.status)
        return {"success": True, "items": items, "has_more": has_more}

    @router.post("/tasks/{task_ref}/manage", response_model=None)
    async def manage(task_ref: str, request: Request) -> dict[str, object] | JSONResponse:
        try:
            return await _manage(task_ref, request)
        except _RequestError as exc:
            return _failure(exc.message, exc.code, exc.status)

    return router
