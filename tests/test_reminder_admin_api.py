"""Authenticated reminder management and durable state safety at the HTTP boundary."""

from __future__ import annotations

import json
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

from plugins.llm_chat import reminder_webui_api as api, reminder_admin_queries, reminder_admin_inspection
from plugins.llm_chat.models import (
    AgentTurn,
    ChatScope,
    AgentEvent,
    ReminderTask,
    ContextSession,
    ReminderMutation,
    ReminderOccurrence,
)
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
    monkeypatch.setattr(reminder_admin_queries, "get_session", factory)
    monkeypatch.setattr(reminder_admin_inspection, "get_session", factory)
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


@pytest.mark.asyncio
async def test_chat_session_filters_use_creation_turn_and_keep_every_orphan_reachable(admin):
    app, factory, (first, second) = admin
    async with factory() as db:
        old = ContextSession(scope_id=first, session_ref="chat_old", sequence=1, status="closed", purpose="chat")
        current = ContextSession(scope_id=first, session_ref="chat_new", sequence=2, status="active", purpose="chat")
        other = ContextSession(scope_id=second, session_ref="chat_other", sequence=1, purpose="chat")
        db.add_all([old, current, other])
        await db.flush()
        turns = [
            AgentTurn(
                session_id=session.id,
                sequence=1,
                turn_ref=f"turn_{session.session_ref}",
                trigger_message_id=f"msg_{session.session_ref}",
                user_id="unified-user",
                user_name="用户",
            )
            for session in (old, current, other)
        ]
        db.add_all(turns)
        await db.flush()
        db.add(
            AgentEvent(
                turn_id=turns[0].id,
                sequence=1,
                event_type="user_input",
                payload_json=json.dumps({"content": "请提醒支付 50% api_key=LEAK /opt/chtholly/private"}),
            )
        )
        for ref, scope_id, account, channel, source, status, goal in (
            ("task_old", first, "bot-1", "group-1", turns[0], "active", "Pay 50% by Friday"),
            ("task_new", first, "bot-1", "group-1", turns[1], "paused", "Book appointment"),
            ("task_unlinked", first, "bot-1", "group-1", turns[2], "active", "Fix_%_literal"),
            ("task_other", second, "bot-2", "group-2", turns[2], "completed", "Other reminder"),
            ("task_orphan", 999999, "bot-1", "group-1", turns[0], "cancelled", "Missing scope"),
            ("task_forged", first, "bot-2", "group-1", turns[0], "active", "Foreign account pointer"),
        ):
            db.add(
                ReminderTask(
                    task_ref=ref,
                    source_scope_id=scope_id,
                    platform="qq",
                    account_id=account,
                    source_channel_id=channel,
                    unified_user_id="unified-user",
                    platform_user_id="qq-10001",
                    source_turn_ref=str(source.id),
                    source_event_ref=source.trigger_message_id,
                    goal=goal,
                    status=status,
                    due_at=datetime.utcnow() + timedelta(hours=1),
                )
            )
        await db.commit()
    root = "/api/llm-chat/reminders"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        sessions = (await client.get(f"{root}/sessions", params={"scope_ref": "scope_first", "limit": 1})).json()
        assert sessions["total_count"] == 2
        assert sessions["has_more"]
        next_page = (await client.get(f"{root}/sessions", params={"scope_ref": "scope_first", "offset": 1})).json()
        assert {item["session_ref"]: item["task_count"] for item in sessions["items"] + next_page["items"]} == {
            "chat_old": 1,
            "chat_new": 1,
        }
        assert (await client.get(f"{root}/sessions", params={"scope_ref": "all"})).status_code == 400
        assert (
            await client.get(f"{root}/tasks", params={"scope_ref": "scope_first", "session_ref": "chat_other"})
        ).status_code == 404
        old_tasks = (
            await client.get(f"{root}/tasks", params={"scope_ref": "scope_first", "session_ref": "chat_old"})
        ).json()
        assert old_tasks["total_count"] == 1
        assert [item["task_ref"] for item in old_tasks["items"]] == ["task_old"]
        assert old_tasks["items"][0]["source_session_ref"] == "chat_old"
        unlinked = (
            await client.get(f"{root}/tasks", params={"scope_ref": "scope_first", "session_ref": "unlinked"})
        ).json()
        assert [item["task_ref"] for item in unlinked["items"]] == ["task_unlinked"]
        orphan = (await client.get(f"{root}/tasks", params={"scope_ref": "unassigned"})).json()
        assert {item["task_ref"] for item in orphan["items"]} == {"task_orphan", "task_forged"}
        all_tasks = (await client.get(f"{root}/tasks", params={"limit": 2})).json()
        assert all_tasks["total_count"] == 6
        assert all_tasks["has_more"]
        assert (await client.get(f"{root}/tasks", params={"offset": 10001})).json()["total_count"] == 6
        by_status = (await client.get(f"{root}/tasks", params={"scope_ref": "scope_first", "status": "paused"})).json()
        assert [item["task_ref"] for item in by_status["items"]] == ["task_new"]
        by_percent = (await client.get(f"{root}/tasks", params={"q": "50%"})).json()
        assert [item["task_ref"] for item in by_percent["items"]] == ["task_old"]
        by_underscore = (await client.get(f"{root}/tasks", params={"q": "_%_literal"})).json()
        assert [item["task_ref"] for item in by_underscore["items"]] == ["task_unlinked"]
        by_owner = (await client.get(f"{root}/tasks", params={"scope_ref": "scope_second", "q": "unified-user"})).json()
        assert [item["task_ref"] for item in by_owner["items"]] == ["task_other"]
        source = (await client.get(f"{root}/tasks/task_old")).json()["item"]["source"]
        assert source["session_ref"] == "chat_old"
        assert source["turn_ref"] == "turn_chat_old"
        assert "50%" in source["input_preview"]
        assert "LEAK" not in source["input_preview"]
        assert "/opt/" not in source["input_preview"]
        assert (await client.get(f"{root}/tasks/task_orphan")).json()["item"]["source"] is None
        assert (await client.get(f"{root}/tasks/task_unlinked")).json()["item"]["source"]["session_ref"] is None
        forbidden = await client.post(
            f"{root}/tasks/task_forged/manage",
            json={"scope_ref": "scope_first", "revision": 0, "action": "pause"},
            headers=headers(),
        )
        assert forbidden.status_code == 404


@pytest.mark.asyncio
async def test_occurrence_history_inspection_uses_bound_reminder_turns_not_chat_or_raw_audit(admin):
    app, factory, (source_scope, other_scope) = admin
    await seed(factory, source_scope)
    async with factory() as db:
        task = await db.scalar(select(ReminderTask).where(ReminderTask.task_ref == "task_a"))
        task.source_channel_id = "group-1"
        task.delivery_target_type = "dm"
        task.delivery_channel_id = ""
        task.delivery_user_id = "qq-10001"
        dm_scope = ChatScope(scope_ref="dm_scope", platform="qq", account_id="bot-1", channel_id="dm-10001")
        db.add(dm_scope)
        await db.flush()
        historical = ContextSession(
            scope_id=dm_scope.id,
            session_ref="reminder_original",
            purpose="reminder",
            reminder_task_ref="task_a",
            sequence=1,
            status="closed",
            model_name="historical-model",
            persona_hash="historical-persona",
            system_version="historical-system",
        )
        rollover = ContextSession(
            scope_id=dm_scope.id,
            session_ref="reminder_rollover",
            purpose="reminder",
            reminder_task_ref="task_a",
            sequence=2,
            status="active",
            model_name="next-model",
        )
        foreign = ContextSession(
            scope_id=other_scope,
            session_ref="reminder_foreign",
            purpose="reminder",
            reminder_task_ref="task_a",
            sequence=1,
        )
        db.add_all([historical, rollover, foreign])
        await db.flush()
        turns = [
            AgentTurn(
                session_id=session.id,
                sequence=1,
                turn_ref=f"run_{index}",
                trigger_kind="scheduled",
                user_id="unified-user",
                user_name="用户",
            )
            for index, session in enumerate((historical, rollover, foreign), start=1)
        ]
        db.add_all(turns)
        await db.flush()
        now = datetime.utcnow()
        for sequence, state, turn_ref, confirmed, transport in (
            (1, "completed", "run_1", 1, True),
            (2, "completed", "run_2", 1, True),
            (3, "unknown", "", 0, True),
            (4, "unknown", "run_3", 0, True),
        ):
            db.add(
                ReminderOccurrence(
                    task_ref="task_a",
                    sequence=sequence,
                    scheduled_at=now,
                    state=state,
                    turn_ref=turn_ref,
                    confirmed_count=confirmed,
                    transport_started=transport,
                    failure_reason="transport_result_unknown" if state == "unknown" else "",
                )
            )
        payloads = [
            (
                "reminder_trigger",
                {
                    "occurrence_sequence": 1,
                    "scheduled_at": "2026-09-29T10:00:00+00:00",
                    "now": "2026-09-29T10:00:03+00:00",
                    "late_by_seconds": 3,
                    "mode": "once",
                    "background": "api_key=NEVER_EXPOSE",
                },
                "started",
                "none",
            ),
            (
                "context_snapshot",
                {
                    "selection": {
                        "estimated_tokens": 777,
                        "included_turn_refs": ["opaque-turn"],
                        "ordinary_chat": {
                            "status": "present",
                            "session_ref": "chat_at_wakeup",
                            "included_turn_refs": ["chat_turn_after_creation"],
                        },
                    },
                    "budgets": {"max_input_tokens": 1200, "output_reserve_tokens": 100},
                    "blocks": [{"content": "SECRET_SYSTEM_PROMPT /opt/chtholly/secret"}],
                    "capture_status": "complete",
                },
                "captured",
                "none",
            ),
            (
                "model_request",
                {
                    "request_id": "model-call",
                    "model": "historical-model",
                    "messages": [{"role": "system", "content": "SECRET_SYSTEM_PROMPT"}],
                },
                "started",
                "none",
            ),
            (
                "model_response",
                {
                    "request_id": "model-call",
                    "content": "Observed output",
                    "usage": {"input_tokens": 17, "output_tokens": 3, "total_tokens": 20},
                },
                "confirmed",
                "none",
            ),
            ("assistant_tool_call", {"audit_arguments": {"data": {"token": "PRIVATE_TOOL_TOKEN"}}}, "started", "none"),
            ("tool_result", {"audit_result": {"data": {"receipt_id": "PRIVATE_RECEIPT"}}}, "confirmed", "confirmed"),
            (
                "message_delivery",
                {
                    "content": "已确认投递",
                    "confirmed_at": "2026-09-29T10:00:08+00:00",
                    "receipt_id": "PRIVATE_RECEIPT",
                    "media": [{"path": "C:\\private\\file"}],
                },
                "confirmed",
                "confirmed",
            ),
            (
                "reminder_decision",
                {"task_ref": "task_a", "status": "completed", "next_due_at": "", "revision": 1, "reason": ""},
                "confirmed",
                "confirmed",
            ),
        ]
        for sequence, (kind, payload, status, effect) in enumerate(payloads, start=1):
            db.add(
                AgentEvent(
                    turn_id=turns[0].id,
                    sequence=sequence,
                    event_type=kind,
                    execution_ref="private-op" if kind in ("assistant_tool_call", "tool_result") else "",
                    tool_name="send_msg" if kind in ("assistant_tool_call", "tool_result") else "",
                    status=status,
                    effect=effect,
                    payload_json=json.dumps(payload, ensure_ascii=False),
                )
            )
        db.add(
            AgentEvent(
                turn_id=turns[1].id,
                sequence=1,
                event_type="reminder_trigger",
                status="started",
                payload_json=json.dumps(
                    {"occurrence_sequence": 2, "mode": "adaptive", "scheduled_at": "2026-09-30T12:00:00Z"}
                ),
            )
        )
        await db.commit()
    async with factory() as db:
        event_refs = dict(
            (
                await db.execute(
                    select(AgentEvent.event_type, AgentEvent.event_ref).where(AgentEvent.turn_id == turns[0].id)
                )
            ).all()
        )
    root = "/api/llm-chat/reminders/tasks/task_a/occurrences"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        page = (await client.get(root, params={"limit": 2})).json()
        assert page["total_count"] == 4
        assert page["has_more"]
        assert [item["sequence"] for item in page["items"]] == [4, 3]
        assert page["items"][0]["turn_ref"] is None
        assert page["items"][1]["confirmed_count"] == 0
        unknown = (await client.get(f"{root}/3")).json()["item"]
        assert unknown["state"] == "unknown"
        assert unknown["transport_started"]
        assert unknown["confirmed_count"] == 0
        assert unknown["inspection"]["turn"] is None
        assert unknown["inspection"]["outputs"] == []
        assert unknown["inspection"]["usage"]["source"] == "not_recorded"
        assert unknown["trigger"] is None
        assert unknown["decision"] is None
        assert unknown["environment"] is None
        assert (await client.get(f"{root}/4")).json()["item"]["inspection"]["turn"] is None
        old = (await client.get(f"{root}/1")).json()["item"]
        assert old["environment"]["session_ref"] == "reminder_original"
        assert old["environment"]["model"] == "historical-model"
        assert old["trigger"]["late_by_seconds"] == 3
        assert old["decision"]["status"] == "completed"
        assert old["inspection"]["context"]["estimated_tokens"] == 777
        assert old["inspection"]["context"]["capture_status"] == "complete"
        assert old["inspection"]["context"]["event_ref"] == event_refs["context_snapshot"]
        assert old["inspection"]["context"]["selection"]["ordinary_chat"] == {
            "status": "present",
            "session_ref": "chat_at_wakeup",
            "included_turn_refs": ["chat_turn_after_creation"],
        }
        assert old["inspection"]["context"]["selection"]["included_turn_refs"] == ["opaque-turn"]
        assert old["inspection"]["model_calls"][0]["model"] == "historical-model"
        assert old["inspection"]["model_calls"][0]["request_event_ref"] == event_refs["model_request"]
        assert old["inspection"]["usage"]["total_tokens"] == 20
        assert old["inspection"]["outputs"][0]["content"] == "已确认投递"
        assert old["inspection"]["tool_calls"][0]["tool_name"] == "send_msg"
        assert old["inspection"]["tool_calls"][0]["call_event_ref"] == event_refs["assistant_tool_call"]
        serialized = json.dumps(old, ensure_ascii=False)
        assert not any(
            private in serialized
            for private in (
                "PRIVATE_RECEIPT",
                "PRIVATE_TOOL_TOKEN",
                "SECRET_SYSTEM_PROMPT",
                "NEVER_EXPOSE",
                "C:\\private",
                "private-op",
            )
        )
        rolled = (await client.get(f"{root}/2")).json()["item"]
        assert rolled["environment"]["session_ref"] == "reminder_rollover"
        assert rolled["trigger"]["mode"] == "adaptive"
        assert rolled["inspection"]["outputs"] == []
        assert (await client.get(f"{root}/5")).status_code == 404
        assert (await client.get(f"{root}", params={"offset": 10001})).json()["total_count"] == 4
