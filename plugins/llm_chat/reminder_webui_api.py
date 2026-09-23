"""Authenticated, read-only WebUI projection of reminder tasks.

WebUI password sessions do not identify a bot account or reminder owner. Do not
expose owner-scoped store mutations (or accept an owner from HTTP input) here.
"""

from __future__ import annotations

from typing import Annotated
from pathlib import Path
from secrets import token_urlsafe
from datetime import datetime, timezone

from fastapi import Query, Depends, APIRouter, HTTPException
from sqlalchemy import and_, func, select
from fastapi.responses import HTMLResponse, JSONResponse
from entari_plugin_database import get_session
from entari_plugin_webui.api.deps import require_auth
from entari_plugin_webui.core.session import Session
from entari_plugin_webui.core.security import is_local_mode

from utils.webui_theme import themed_assets

from .models import ReminderTask, ReminderOccurrence

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


def _authenticated(session: Session | None = Depends(require_auth)) -> Session:
    # require_auth returns None in local/no-password mode. That is not an
    # authenticated session, even if the browser can load other WebUI pages.
    if is_local_mode() or session is None:
        raise HTTPException(status_code=403, detail="需要启用 WebUI 登录后查看提醒")
    return session


def _utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _project(task: ReminderTask, occurrence: ReminderOccurrence | None) -> dict[str, object]:
    # Explicit allowlist: never serialize ORM objects, owner/scope identifiers,
    # source refs, lease tokens, receipts or mutation operation IDs.
    state = occurrence.state if occurrence is not None else None
    return {
        "goal": task.goal[:1000],
        "mode": task.mode if task.mode in {"once", "adaptive"} else "unknown",
        "origin": task.origin if task.origin in {"user", "autonomous"} else "unknown",
        "delivery": task.delivery_target_type if task.delivery_target_type in {"channel", "dm"} else "unknown",
        "status": task.status if task.status in _STATUSES else "unknown",
        "due_at": _utc(task.due_at),
        "timezone": task.timezone[:64],
        "created_at": _utc(task.created_at),
        "updated_at": _utc(task.updated_at),
        "occurrence": None
        if occurrence is None
        else {
            "state": state if state in _OCCURRENCE_STATES else "unknown",
            "scheduled_at": _utc(occurrence.scheduled_at),
            "started_at": _utc(occurrence.started_at),
            "finished_at": _utc(occurrence.finished_at),
            "confirmed_count": max(0, occurrence.confirmed_count),
        },
    }


async def _list_projections(limit: int, offset: int) -> tuple[list[dict[str, object]], bool]:
    async with get_session() as db:
        tasks = list(
            (
                await db.execute(
                    select(ReminderTask)
                    .order_by(ReminderTask.updated_at.desc(), ReminderTask.id.desc())
                    .offset(offset)
                    .limit(limit + 1)
                )
            )
            .scalars()
            .all()
        )
        if not tasks:
            return [], False
        has_more = len(tasks) > limit
        tasks = tasks[:limit]
        task_refs = [task.task_ref for task in tasks]
        latest = (
            select(ReminderOccurrence.task_ref, func.max(ReminderOccurrence.sequence).label("sequence"))
            .where(ReminderOccurrence.task_ref.in_(task_refs))
            .group_by(ReminderOccurrence.task_ref)
            .subquery()
        )
        occurrences = (
            (
                await db.execute(
                    select(ReminderOccurrence).join(
                        latest,
                        and_(
                            ReminderOccurrence.task_ref == latest.c.task_ref,
                            ReminderOccurrence.sequence == latest.c.sequence,
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
        by_ref = {item.task_ref: item for item in occurrences}
        return [_project(task, by_ref.get(task.task_ref)) for task in tasks], has_more


def create_reminder_webui_router(*, asset_dir: Path) -> APIRouter:
    router = APIRouter(prefix=_API_PREFIX, tags=["reminders"], dependencies=[Depends(_authenticated)])

    @router.get("/page", include_in_schema=False, response_model=None)
    async def page() -> HTMLResponse | JSONResponse:
        nonce = token_urlsafe(24)
        try:
            document = (asset_dir / "index.html").read_text(encoding="utf-8")
            stylesheet, script = themed_assets(asset_dir)
        except (OSError, UnicodeError):
            return JSONResponse(
                {"success": False, "code": "page_unavailable", "message": "提醒页面暂不可用"},
                status_code=503,
                headers=_PAGE_HEADERS,
            )
        document = document.replace("<!-- REMINDER_STYLES -->", f'<style nonce="{nonce}">{stylesheet}</style>')
        document = document.replace("<!-- REMINDER_SCRIPT -->", f'<script nonce="{nonce}">{script}</script>')
        headers = dict(_PAGE_HEADERS)
        headers["Content-Security-Policy"] = (
            f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
            "connect-src 'self'; img-src 'self' blob:; base-uri 'none'; object-src 'none'; "
            "frame-ancestors 'self'; form-action 'none'"
        )
        return HTMLResponse(document, headers=headers)

    @router.get("/tasks", response_model=None)
    async def tasks(
        limit: Annotated[int, Query(ge=1, le=100)] = 40,
        offset: Annotated[int, Query(ge=0, le=10000)] = 0,
    ) -> dict[str, object]:
        items, has_more = await _list_projections(limit, offset)
        return {"success": True, "items": items, "has_more": has_more}

    return router
