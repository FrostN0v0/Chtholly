"""Authenticated reminder management and durable state safety at the HTTP boundary."""

from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from arclet.entari.config import EntariConfig
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

if not hasattr(EntariConfig, "instance"):
    setattr(
        EntariConfig, "instance", EntariConfig.load(Path(__file__).resolve().parents[1] / "entari.full.example.yml")
    )

from entari_plugin_database import Base

from plugins.llm_chat import reminder_webui_api as api
from plugins.llm_chat.models import ChatScope, ReminderTask, ReminderMutation, ReminderOccurrence
from plugins.llm_chat.reminders import store

ORIGIN = "https://webui.example"


@pytest.fixture
async def admin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'admin.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(store, "get_session", factory)
    monkeypatch.setattr(api, "get_session", factory)
    monkeypatch.setattr(api, "is_local_mode", lambda: False)
    app = FastAPI()
    app.include_router(api.create_reminder_webui_router(asset_dir=tmp_path))
    app.dependency_overrides[api.require_auth] = lambda: object()
    async with factory() as db:
        first = ChatScope(
            scope_ref="scope_first", platform="qq", account_id="bot-1", channel_id="group-1", display_name="Group One"
        )
        second = ChatScope(
            scope_ref="scope_second", platform="qq", account_id="bot-2", channel_id="group-2", display_name="Group Two"
        )
        db.add_all([first, second])
        await db.commit()
        await db.refresh(first)
        await db.refresh(second)
        scope_ids = first.id, second.id
    try:
        yield app, factory, scope_ids
    finally:
        await engine.dispose()


def headers(origin: str = ORIGIN, **extra: str) -> dict[str, str]:
    return {"Origin": origin, "Content-Type": "application/json", **extra}


async def seed(
    factory,
    scope_id: int | None,
    *,
    account: str = "bot-1",
    ref: str = "task_a",
    due_minutes: int = 30,
    status: str = "active",
) -> None:
    async with factory() as db:
        db.add(
            ReminderTask(
                task_ref=ref,
                source_scope_id=scope_id,
                platform="qq",
                account_id=account,
                unified_user_id="unified-user",
                platform_user_id="qq-10001",
                goal="Original goal",
                due_at=datetime.utcnow() + timedelta(minutes=due_minutes),
                status=status,
            )
        )
        await db.commit()


@pytest.mark.asyncio
async def test_scope_listing_and_admin_projection_include_orphans_without_private_ledger(admin):
    app, factory, (first, second) = admin
    await seed(factory, first)
    await seed(factory, second, account="bot-2", ref="task_b")
    await seed(factory, None, ref="task_orphan")
    async with factory() as db:
        db.add(
            ChatScope(
                scope_ref="scope_empty",
                platform="qq",
                account_id="bot-3",
                channel_id="group-empty",
                display_name="Empty Group",
            )
        )
        await db.commit()
    async with factory() as db:
        db.add(
            ReminderOccurrence(
                task_ref="task_a",
                sequence=1,
                scheduled_at=datetime.utcnow(),
                state="confirmed",
                transport_started=True,
                confirmed_count=2,
                lease_token="private-lease",
                confirmed_receipts_json='["private-receipt"]',
            )
        )
        await db.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        scopes = (await client.get("/api/llm-chat/reminders/scopes")).json()["items"]
        assert {row["scope_ref"]: row["task_count"] for row in scopes} == {"scope_first": 1, "scope_second": 1}
        selected = (await client.get("/api/llm-chat/reminders/tasks", params={"scope_ref": "scope_first"})).json()
        assert [row["task_ref"] for row in selected["items"]] == ["task_a"]
        all_items = (await client.get("/api/llm-chat/reminders/tasks", params={"scope_ref": "all"})).json()["items"]
        assert {row["task_ref"]: row["scope_ref"] for row in all_items} == {
            "task_a": "scope_first",
            "task_b": "scope_second",
            "task_orphan": None,
        }
        first_item = next(row for row in all_items if row["task_ref"] == "task_a")
        assert first_item["owner_user_id"] == "unified-user"
        assert first_item["occurrence"]["confirmed_count"] == 2
        assert "private-lease" not in str(first_item)
        assert "private-receipt" not in str(first_item)
        assert (await client.get("/api/llm-chat/reminders/tasks", params={"scope_ref": "forged"})).status_code == 404


@pytest.mark.asyncio
async def test_write_rejects_unauthorized_cross_origin_forged_scope_bad_fields_and_stale_revision(admin, monkeypatch):
    app, factory, (first, second) = admin
    await seed(factory, first)
    endpoint = "/api/llm-chat/reminders/tasks/task_a/manage"
    body = {"scope_ref": "scope_first", "revision": 0, "action": "update", "goal": "Changed"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        monkeypatch.setattr(api, "is_local_mode", lambda: True)
        assert (await client.post(endpoint, json=body, headers=headers())).status_code == 403
        monkeypatch.setattr(api, "is_local_mode", lambda: False)
        app.dependency_overrides[api.require_auth] = lambda: None
        assert (await client.post(endpoint, json=body, headers=headers())).status_code == 403
        app.dependency_overrides[api.require_auth] = lambda: object()
        for supplied in (
            headers("https://attacker.example"),
            {"Content-Type": "application/json"},
            headers(**{"Sec-Fetch-Site": "cross-site"}),
        ):
            assert (await client.post(endpoint, json=body, headers=supplied)).status_code == 403
        assert (
            await client.post(endpoint, json={**body, "scope_ref": "scope_second"}, headers=headers())
        ).status_code == 404
        assert (
            await client.post(endpoint, json={**body, "unified_user_id": "attacker"}, headers=headers())
        ).status_code == 400
        assert (await client.post(endpoint, json={**body, "revision": True}, headers=headers())).status_code == 400
        response = await client.post(endpoint, json=body, headers=headers())
        assert response.status_code == 200
        assert response.json()["item"]["revision"] == 1
        assert (await client.post(endpoint, json=body, headers=headers())).status_code == 409
    async with factory() as db:
        task = await db.scalar(select(ReminderTask).where(ReminderTask.task_ref == "task_a"))
        mutations = (
            (await db.execute(select(ReminderMutation).where(ReminderMutation.task_ref == "task_a"))).scalars().all()
        )
        assert task.goal == "Changed"
        assert task.revision == 1
        assert len(mutations) == 1


@pytest.mark.asyncio
async def test_commit_acknowledgement_failure_confirms_once_without_replaying(admin, monkeypatch):
    app, factory, (first, _) = admin
    await seed(factory, first)
    original = store.update_task
    calls = 0

    async def uncertain(*args, **kwargs):
        nonlocal calls
        calls += 1
        await original(*args, **kwargs)
        raise OSError("lost acknowledgement")

    monkeypatch.setattr(store, "update_task", uncertain)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        response = await client.post(
            "/api/llm-chat/reminders/tasks/task_a/manage",
            json={"scope_ref": "scope_first", "revision": 0, "action": "update", "goal": "Durable"},
            headers=headers(),
        )
        assert response.status_code == 200
        assert response.json()["item"]["goal"] == "Durable"
    assert calls == 1
    async with factory() as db:
        assert await db.scalar(select(ReminderTask.revision).where(ReminderTask.task_ref == "task_a")) == 1
        assert len((await db.execute(select(ReminderMutation))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_unconfirmed_write_failure_remains_unknown_and_is_never_replayed(admin, monkeypatch):
    app, factory, (first, _) = admin
    await seed(factory, first)
    calls = 0

    async def lost(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise OSError("database acknowledgement unavailable")

    monkeypatch.setattr(store, "update_task", lost)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        response = await client.post(
            "/api/llm-chat/reminders/tasks/task_a/manage",
            json={"scope_ref": "scope_first", "revision": 0, "action": "update", "goal": "Unconfirmed"},
            headers=headers(),
        )
        assert response.status_code == 503
        assert response.json()["code"] == "mutation_unknown"
    assert calls == 1
    async with factory() as db:
        assert await db.scalar(select(ReminderTask.revision).where(ReminderTask.task_ref == "task_a")) == 0


@pytest.mark.parametrize(("state", "transport_started", "confirmed_count"), [("paused", True, 1), ("unknown", True, 0)])
@pytest.mark.asyncio
async def test_confirmed_or_unknown_past_due_requires_future_reschedule(
    admin, state, transport_started, confirmed_count
):
    app, factory, (first, _) = admin
    await seed(factory, first, status="paused", due_minutes=-10)
    async with factory() as db:
        db.add(
            ReminderOccurrence(
                task_ref="task_a",
                sequence=1,
                scheduled_at=datetime.utcnow() - timedelta(minutes=10),
                state=state,
                transport_started=transport_started,
                confirmed_count=confirmed_count,
            )
        )
        await db.commit()
    endpoint = "/api/llm-chat/reminders/tasks/task_a/manage"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        base = {"scope_ref": "scope_first", "revision": 0}
        blocked = await client.post(endpoint, json={**base, "action": "resume"}, headers=headers())
        assert blocked.status_code == 409
        wrong = await client.post(
            endpoint, json={**base, "action": "update", "schedule": "2026-01-01T10:00:00"}, headers=headers()
        )
        assert wrong.status_code == 400
        schedule = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        updated = await client.post(
            endpoint, json={**base, "action": "update", "schedule": schedule}, headers=headers()
        )
        assert updated.status_code == 200
        assert updated.json()["item"]["status"] == "paused"
        resumed = await client.post(
            endpoint, json={"scope_ref": "scope_first", "revision": 1, "action": "resume"}, headers=headers()
        )
        assert resumed.status_code == 200
        assert resumed.json()["item"]["status"] == "active"


@pytest.mark.asyncio
async def test_terminal_task_is_visible_but_cannot_be_mutated_through_admin_api(admin):
    app, factory, (first, _) = admin
    await seed(factory, first, status="completed")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        response = await client.post(
            "/api/llm-chat/reminders/tasks/task_a/manage",
            json={"scope_ref": "scope_first", "revision": 0, "action": "update", "goal": "Rewritten"},
            headers=headers(),
        )
        assert response.status_code == 409
        assert response.json()["code"] == "task_terminal"
    async with factory() as db:
        task = await db.scalar(select(ReminderTask).where(ReminderTask.task_ref == "task_a"))
        assert (task.goal, task.revision) == ("Original goal", 0)
