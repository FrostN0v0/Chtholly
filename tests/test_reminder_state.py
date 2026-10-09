"""Persistence and lease invariants for durable reminders."""

from __future__ import annotations

from types import SimpleNamespace
import asyncio
from pathlib import Path
from datetime import datetime, timezone, timedelta

import pytest
from arclet.entari.config import EntariConfig
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

if not hasattr(EntariConfig, "instance"):
    setattr(
        EntariConfig, "instance", EntariConfig.load(Path(__file__).resolve().parents[1] / "entari.full.example.yml")
    )

from entari_plugin_database import Base

from plugins.llm_chat.models import ReminderOccurrence
from plugins.llm_chat.reminders import store


@pytest.fixture
async def reminder_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'reminders.db'}", connect_args={"timeout": 15})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(store, "get_session", factory)
    try:
        yield SimpleNamespace(engine=engine, factory=factory)
    finally:
        await engine.dispose()


UTC = timezone.utc


def due(minutes: int = 0) -> datetime:
    return datetime.now(UTC) + timedelta(minutes=minutes)


async def create_one(**overrides):
    args = {
        "operation_id": "create-op-1",
        "platform": "qq",
        "account_id": "bot-1",
        "unified_user_id": "user-1",
        "platform_user_id": "p-user-1",
        "source_channel_id": "group-1",
        "goal": "check in",
        "due_at": due(-1),
    }
    args.update(overrides)
    return await store.create_task(**args)


@pytest.mark.asyncio
async def test_create_is_idempotent_and_listing_is_owner_scoped(reminder_store):
    first = await create_one()
    replay = await create_one()
    assert replay.task_ref == first.task_ref
    assert replay.revision == first.revision == 0
    assert len(await store.list_tasks(account_id="bot-1", unified_user_id="user-1", platform="qq")) == 1
    assert await store.list_tasks(account_id="bot-1", unified_user_id="other", platform="qq") == []
    assert await store.get_task(first.task_ref, account_id="bot-1", unified_user_id="other", platform="qq") is None


@pytest.mark.asyncio
async def test_concurrent_claim_has_one_winner(reminder_store):
    task = await create_one()
    claims = await asyncio.gather(
        store.try_claim_due(task.task_ref, due(-1), lease_seconds=60),
        store.try_claim_due(task.task_ref, due(-1), lease_seconds=60),
    )
    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    async with reminder_store.factory() as db:
        rows = (await db.execute(__import__("sqlalchemy").select(ReminderOccurrence))).scalars().all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_stale_revision_cannot_update(reminder_store):
    task = await create_one()
    updated = await store.update_task(
        task.task_ref,
        operation_id="update-op-1",
        expected_revision=0,
        account_id="bot-1",
        unified_user_id="user-1",
        platform="qq",
        goal="changed",
    )
    assert updated.revision == 1
    with pytest.raises(store.ReminderConflict):
        await store.update_task(
            task.task_ref,
            operation_id="update-op-2",
            expected_revision=0,
            account_id="bot-1",
            unified_user_id="user-1",
            platform="qq",
            goal="stale",
        )


@pytest.mark.asyncio
async def test_external_boundary_expiry_is_unknown_and_not_replayed(reminder_store):
    task = await create_one()
    claim = await store.try_claim_due(task.task_ref, due(-1), lease_seconds=1)
    assert claim is not None
    assert await store.record_model_started(claim)
    assert await store.record_external_started(claim, kind="transport")
    assert await store.recover_stale(now=claim.lease_deadline + timedelta(seconds=1)) == 1
    assert (
        await store.try_claim_due(task.task_ref, claim.lease_deadline + timedelta(seconds=2), lease_seconds=60) is None
    )
    assert not await store.record_confirmed(claim, receipt_id="late")
    with pytest.raises(store.ReminderConflict):
        await store.finalize_claim(claim, status="completed")


@pytest.mark.asyncio
async def test_cancel_wins_stale_claim(reminder_store):
    task = await create_one()
    claim = await store.try_claim_due(task.task_ref, due(-1), lease_seconds=60)
    assert claim is not None
    cancelled = await store.cancel_task(
        task.task_ref,
        operation_id="cancel-op-1",
        expected_revision=claim.revision,
        account_id="bot-1",
        unified_user_id="user-1",
        platform="qq",
    )
    assert cancelled.status == "cancelled"
    assert not await store.record_model_started(claim)
    with pytest.raises(store.ReminderConflict):
        await store.finalize_claim(claim, status="completed")


@pytest.mark.asyncio
async def test_pre_model_recovery_and_multiple_occurrences(reminder_store):
    task = await create_one(mode="adaptive")
    claim = await store.try_claim_due(task.task_ref, due(-1), lease_seconds=1)
    assert claim is not None
    assert await store.recover_stale(now=claim.lease_deadline + timedelta(seconds=1)) == 1
    claim2 = await store.try_claim_due(task.task_ref, claim.lease_deadline + timedelta(seconds=2), lease_seconds=60)
    assert claim2 is not None
    assert claim2.occurrence_sequence == 2
    assert await store.record_model_started(claim2)
    next_time = claim2.lease_deadline + timedelta(minutes=5)
    continued = await store.finalize_claim(claim2, next_due_at=next_time, continuation_state="step-2")
    assert continued.status == "active"
    assert continued.current_occurrence == 2
    claim3 = await store.try_claim_due(task.task_ref, next_time, lease_seconds=60)
    assert claim3 is not None
    assert claim3.occurrence_sequence == 3
    finished = await store.finalize_claim(claim3, status="completed")
    assert finished.status == "completed"
