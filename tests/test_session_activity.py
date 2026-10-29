"""Ordinary conversation activity ownership and stable bounded pagination."""

from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from arclet.entari.config import EntariConfig

if not hasattr(EntariConfig, "instance"):
    EntariConfig.instance = EntariConfig.load(Path(__file__).resolve().parents[1] / "entari.full.example.yml")

from fastapi import FastAPI
from sqlalchemy import func, select
from entari_plugin_database import Base
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from plugins.llm_chat import agent_admin, personality, agent_events, session_manager, session_inspection
from plugins.llm_chat.config import LLMChatConfig
from plugins.llm_chat.models import (
    AgentTurn,
    ChatScope,
    AgentEvent,
    ReminderTask,
    ContextSession,
    ReminderOccurrence,
    SessionPersonaSnapshot,
)
from plugins.llm_chat.agent_admin import AgentAdminError, AgentAdminService
from plugins.llm_chat.agent_webui_api import create_agent_sessions_router
from plugins.llm_chat.companion.models import CompanionMember, CompanionReview, CompanionContact, CompanionChatEvidence

AT = datetime(2026, 10, 20, 12)
PREFIX = "/api/llm-chat/sessions"


@pytest_asyncio.fixture
async def activity_db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    for module in (agent_admin, agent_events, personality, session_inspection, session_manager):
        monkeypatch.setattr(module, "get_session", factory)
    service = AgentAdminService(LLMChatConfig(), [])
    try:
        yield factory, service
    finally:
        await engine.dispose()


async def _scope(db, channel="source", account="bot"):
    row = ChatScope(platform="test", account_id=account, channel_id=channel)
    db.add(row)
    await db.flush()
    return row


async def _session(db, scope, *, purpose="chat", persona="alpha", task="", at=AT - timedelta(days=1)):
    sequence = 1 + (
        await db.scalar(
            select(func.coalesce(func.max(ContextSession.sequence), 0)).where(ContextSession.scope_id == scope.id)
        )
    )
    row = ContextSession(scope_id=scope.id, sequence=sequence, purpose=purpose, reminder_task_ref=task, created_at=at)
    db.add(row)
    await db.flush()
    if persona is not None:
        db.add(SessionPersonaSnapshot(session_id=row.id, snapshot_json=json.dumps({"key": persona})))
    return row


async def _turn(db, session, *, trigger="message", user="alice", at=AT, message="message"):
    sequence = 1 + (
        await db.scalar(
            select(func.coalesce(func.max(AgentTurn.sequence), 0)).where(AgentTurn.session_id == session.id)
        )
    )
    row = AgentTurn(
        session_id=session.id,
        sequence=sequence,
        trigger_kind=trigger,
        user_id=user,
        user_name=user,
        trigger_message_id=message,
        status="completed",
        created_at=at,
        finished_at=at,
    )
    db.add(row)
    await db.flush()
    return row


async def _event(db, turn, kind, payload, *, status="recorded", visible=False):
    sequence = 1 + (
        await db.scalar(select(func.coalesce(func.max(AgentEvent.sequence), 0)).where(AgentEvent.turn_id == turn.id))
    )
    row = AgentEvent(
        turn_id=turn.id,
        sequence=sequence,
        event_type=kind,
        payload_json=json.dumps(payload),
        model_visible=visible,
        status=status,
    )
    db.add(row)
    await db.flush()
    return row


async def _review(db, scope, source, ref, *, explicit=True, persona="alpha", evidence=None, at=AT):
    row = CompanionReview(
        lease_token=ref,
        scope_id=scope.id,
        persona_key=persona,
        mode_revision=1,
        context_revision=1,
        life_version=1,
        lease_deadline=at + timedelta(minutes=5),
        budget_day="2026-10-20",
        status="completed",
        started_at=at,
        finished_at=at,
        committed_life_version=2,
    )
    db.add(row)
    native_session = await _session(db, scope, purpose="companion_reflection", persona=persona)
    native = await _turn(db, native_session, trigger="reflection", user="", at=at)
    payload = {"lease_token": ref}
    if explicit:
        payload["source_session_ref"] = source.session_ref if source is not None else None
    await _event(db, native, "companion_reflection", payload, status="started")
    if evidence is not None:
        await _event(db, native, "companion_state", {"members": evidence})
    return row, native


async def _member(db, scope, *, user="alice", platform_user="qq-alice", ref="member_alice", persona="alpha"):
    row = CompanionMember(
        scope_id=scope.id,
        persona_key=persona,
        member_ref=ref,
        user_id=user,
        platform_user_id=platform_user,
        display_name=user,
        last_user_at=AT,
    )
    db.add(row)
    await db.flush()
    return row


async def _contact(db, scope, source, member, ref, *, review="none", at=AT):
    native_session = await _session(db, scope, purpose="companion", persona=member.persona_key)
    native = await _turn(db, native_session, trigger="autonomous", user=member.user_id, at=at)
    if source is not None:
        await _event(
            db,
            native,
            "context_snapshot",
            {
                "selection": {
                    "ordinary_chat": {
                        "status": "present",
                        "session_ref": source.session_ref,
                        "included_turn_refs": [],
                    }
                }
            },
        )
    row = CompanionContact(
        attempt_ref=ref,
        scope_id=scope.id,
        persona_key=member.persona_key,
        mode_revision=1,
        context_revision=1,
        origin_review_token=review,
        member_ref=member.member_ref,
        member_version=0,
        user_id=member.user_id,
        platform_user_id=member.platform_user_id,
        display_name=member.display_name,
        proposal_key=ref,
        identity=ref,
        proposal_json="{}",
        limits_json="{}",
        budget_day="2026-10-20",
        lease_token="private-contact-lease",
        lease_deadline=AT,
        turn_id=native.id,
        status="confirmed",
        created_at=at,
    )
    db.add(row)
    await db.flush()
    return row, native


async def _task(db, scope, turn, ref, **overrides):
    values = {
        "task_ref": ref,
        "platform": scope.platform,
        "account_id": scope.account_id,
        "source_scope_id": scope.id,
        "source_channel_id": scope.channel_id,
        "unified_user_id": turn.user_id,
        "platform_user_id": "qq-alice",
        "delivery_target_type": "channel",
        "delivery_channel_id": scope.channel_id,
        "goal": "A recorded reminder",
        "source_turn_ref": str(turn.id),
        "source_event_ref": turn.trigger_message_id,
        "due_at": AT,
        "created_at": AT,
    }
    values.update(overrides)
    row = ReminderTask(**values)
    db.add(row)
    await db.flush()
    return row


async def _all(service, source, *, limit=100):
    items, cursor, seen = [], "", set()
    while True:
        page = await service.list_activity(source.session_ref, limit=limit, cursor=cursor)
        items = page["items"] + items
        cursor = page["next_cursor"]
        if cursor is None:
            return items
        assert cursor not in seen
        seen.add(cursor)
        assert len(seen) < 30


@pytest.mark.asyncio
async def test_existing_endpoint_unions_validated_activity_without_duplicate_occurrence(activity_db):
    factory, service = activity_db
    async with factory() as db:
        scope = await _scope(db)
        destination_scope = await _scope(db, "other-destination")
        source = await _session(db, scope)
        direct = await _turn(db, source)
        member = await _member(db, scope)
        review, reflection = await _review(db, scope, source, "review_owned")
        _, contact = await _contact(db, scope, source, member, "contact_owned", review=review.lease_token)
        task = await _task(db, scope, direct, "task_owned", delivery_channel_id=destination_scope.channel_id)
        native_session = await _session(db, destination_scope, purpose="reminder", task=task.task_ref)
        scheduled = await _turn(db, native_session, trigger="scheduled")
        db.add_all(
            [
                ReminderOccurrence(
                    task_ref=task.task_ref,
                    sequence=1,
                    turn_ref=scheduled.turn_ref,
                    scheduled_at=AT,
                    started_at=AT,
                    state="completed",
                    confirmed_count=1,
                    lease_token="private-reminder-lease",
                    confirmed_receipts_json='["private-receipt"]',
                ),
                ReminderOccurrence(task_ref=task.task_ref, sequence=2, scheduled_at=AT, state="paused"),
                ReminderOccurrence(task_ref=task.task_ref, sequence=3, turn_ref=direct.turn_ref, scheduled_at=AT),
            ]
        )
        await db.commit()
    app = FastAPI()
    app.include_router(create_agent_sessions_router(service, asset_dir=Path("plugins/llm_chat/webui_sessions")))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        native = (await client.get(f"{PREFIX}/sessions/{source.session_ref}/turns")).json()
        assert [row["turn_ref"] for row in native["items"]] == [direct.turn_ref]
        assert "next_cursor" not in native
        response = await client.get(f"{PREFIX}/sessions/{source.session_ref}/turns", params={"view": "activity"})
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["next_cursor"] is None
        items = body["items"]
        assert {item["key"] for item in items} == {
            f"turn:{direct.turn_ref}",
            f"review:{review.lease_token}",
            f"turn:{contact.turn_ref}",
            f"reminder:{task.task_ref}",
            f"turn:{scheduled.turn_ref}",
            f"occurrence:{task.task_ref}:2",
            f"occurrence:{task.task_ref}:3",
        }
        assert f"turn:{reflection.turn_ref}" not in {item["key"] for item in items}
        execution = next(item for item in items if item["key"] == f"turn:{scheduled.turn_ref}")
        assert execution["turn"]["purpose"] == "reminder"
        assert execution["turn"]["trigger_kind"] == "scheduled"
        assert execution["native_session_ref"] == native_session.session_ref
        assert execution["destination"]["scope_ref"] == destination_scope.scope_ref
        assert execution["destination"]["is_source_scope"] is False
        assert execution["reminder"]["source_turn_ref"] == direct.turn_ref
        fallback = [item for item in items if item["kind"] == "reminder_occurrence"]
        assert {item["recording_status"] for item in fallback} == {"missing", "invalid"}
        encoded = json.dumps(body)
        assert "private-reminder-lease" not in encoded
        assert "private-contact-lease" not in encoded
        assert "private-receipt" not in encoded
        assert all(item["scope_ref"] == scope.scope_ref and item["session_ref"] == source.session_ref for item in items)
    paginated = await _all(service, source, limit=2)
    assert len(paginated) == len(items)
    assert {item["key"] for item in paginated} == {item["key"] for item in items}


@pytest.mark.asyncio
async def test_reminder_creation_requires_exact_numeric_scope_account_owner_and_event(activity_db):
    factory, service = activity_db
    async with factory() as db:
        scope = await _scope(db)
        foreign_scope = await _scope(db, "foreign")
        source = await _session(db, scope)
        turn = await _turn(db, source)
        valid = await _task(db, scope, turn, "valid")
        bad_values = [
            {"account_id": "other-bot"},
            {"platform": "other-platform"},
            {"source_scope_id": foreign_scope.id},
            {"source_channel_id": "wrong-channel"},
            {"unified_user_id": "mallory"},
            {"source_event_ref": "wrong-message"},
            {"source_turn_ref": turn.turn_ref},
            {"source_turn_ref": f"0{turn.id}"},
            {"source_turn_ref": "999999"},
        ]
        for number, values in enumerate(bad_values):
            task = await _task(db, scope, turn, f"invalid_{number}", **values)
            db.add(ReminderOccurrence(task_ref=task.task_ref, sequence=1, scheduled_at=AT))
        await db.commit()
    items = await _all(service, source)
    assert [item["reminder"]["task_ref"] for item in items if item["kind"] == "reminder"] == [valid.task_ref]
    assert not any(item["kind"] == "reminder_occurrence" for item in items)


@pytest.mark.asyncio
async def test_review_saved_source_is_historical_and_legacy_evidence_has_real_member_ownership(activity_db):
    factory, service = activity_db
    async with factory() as db:
        scope = await _scope(db)
        foreign_scope = await _scope(db, "foreign")
        source = await _session(db, scope)
        newer = await _session(db, scope)
        foreign = await _session(db, foreign_scope)
        wrong_persona = await _session(db, scope, persona="beta")
        background = await _session(db, scope, purpose="companion")
        direct = await _turn(db, source, at=AT - timedelta(minutes=1))
        alice = await _member(db, scope)
        bob = await _member(db, scope, user="bob", platform_user="qq-bob", ref="member_bob")
        db.add_all(
            [
                CompanionChatEvidence(
                    scope_id=scope.id,
                    persona_key="alpha",
                    turn_id=direct.id,
                    member_ref=alice.member_ref,
                    received_at=AT,
                ),
                CompanionChatEvidence(
                    scope_id=scope.id, persona_key="alpha", turn_id=direct.id, member_ref=bob.member_ref, received_at=AT
                ),
            ]
        )
        await _review(db, scope, source, "saved")
        await _review(db, scope, newer, "newer-only")
        await _review(db, scope, foreign, "wrong-scope")
        await _review(db, scope, wrong_persona, "wrong-persona")
        await _review(db, scope, background, "background-source")
        await _review(
            db,
            scope,
            None,
            "explicit-missing",
            evidence=[
                {
                    "member_ref": alice.member_ref,
                    "known_evidence_turn_ids": [direct.id],
                }
            ],
        )
        await _review(
            db,
            scope,
            None,
            "legacy-valid",
            explicit=False,
            evidence=[
                {
                    "member_ref": alice.member_ref,
                    "known_evidence_turn_ids": [direct.id],
                }
            ],
        )
        await _review(
            db,
            scope,
            None,
            "legacy-forged-member",
            explicit=False,
            evidence=[
                {
                    "member_ref": bob.member_ref,
                    "known_evidence_turn_ids": [direct.id],
                }
            ],
        )
        await _review(db, scope, None, "legacy-missing", explicit=False)
        await _review(db, scope, source, "native-persona-mismatch", persona="beta")
        await db.commit()
    refs = {item["review"]["review_ref"] for item in await _all(service, source) if item["kind"] == "review"}
    assert refs == {"saved", "legacy-valid"}
    newer_refs = {item["review"]["review_ref"] for item in await _all(service, newer) if item["kind"] == "review"}
    assert newer_refs == {"newer-only"}
    archive = await service.companion_reviews(scope.scope_ref, limit=100)
    by_ref = {item["review_ref"]: item for item in archive["items"]}
    assert by_ref["saved"]["activity_session_refs"] == [source.session_ref]
    assert by_ref["legacy-valid"]["activity_association"] == "assigned"
    assert by_ref["newer-only"]["activity_session_refs"] == [newer.session_ref]
    for ref in ("wrong-scope", "explicit-missing", "legacy-forged-member", "legacy-missing"):
        assert by_ref[ref]["activity_association"] == "unassigned"
        assert by_ref[ref]["activity_session_refs"] == []


@pytest.mark.asyncio
async def test_contacts_use_snapshot_or_validated_review_and_reject_wrong_native_member(activity_db):
    factory, service = activity_db
    async with factory() as db:
        scope = await _scope(db)
        source = await _session(db, scope)
        newer = await _session(db, scope)
        member = await _member(db, scope)
        review, _ = await _review(db, scope, source, "origin")
        _, both = await _contact(db, scope, source, member, "both", review=review.lease_token)
        _, origin = await _contact(db, scope, None, member, "origin-only", review=review.lease_token)
        _, later = await _contact(db, scope, newer, member, "later-snapshot", review=review.lease_token)
        invalid, _ = await _contact(db, scope, source, member, "wrong-member")
        invalid.platform_user_id = "someone-else"
        _, unrelated = await _contact(db, scope, newer, member, "unrelated")
        _, bad_trigger = await _contact(db, scope, source, member, "bad-trigger")
        bad_trigger.trigger_kind = "message"
        await db.commit()
    contacts = [item for item in await _all(service, source, limit=1) if item.get("relation") == "contact"]
    assert {item["turn"]["turn_ref"] for item in contacts} == {both.turn_ref, origin.turn_ref, later.turn_ref}
    assert len(contacts) == 3
    assert unrelated.turn_ref not in {item["turn"]["turn_ref"] for item in contacts}
    assert all(item["turn"]["purpose"] == "companion" for item in contacts)


@pytest.mark.asyncio
async def test_activity_cursor_crosses_equal_timestamps_and_ignores_concurrent_backdated_rows(activity_db):
    factory, service = activity_db
    async with factory() as db:
        scope = await _scope(db)
        source = await _session(db, scope)
        other = await _session(db, scope)
        expected = {f"turn:{(await _turn(db, source)).turn_ref}" for _ in range(115)}
        await db.commit()
    first = await service.list_activity(source.session_ref, limit=7)
    cursor = first["next_cursor"]
    assert cursor
    with pytest.raises(AgentAdminError, match="cursor"):
        await service.list_activity(other.session_ref, cursor=cursor)
    with pytest.raises(AgentAdminError, match="cursor"):
        await service.list_activity(source.session_ref, cursor="not-a-cursor")
    async with factory() as db:
        new_turn = await _turn(db, source, at=AT - timedelta(hours=1))
        await db.commit()
    keys = [item["key"] for item in first["items"]]
    while cursor:
        page = await service.list_activity(source.session_ref, limit=7, cursor=cursor)
        assert len(page["items"]) <= 7
        assert [item["key"] for item in page["items"]] == sorted(item["key"] for item in page["items"])
        keys.extend(item["key"] for item in page["items"])
        cursor = page["next_cursor"]
    assert len(keys) == len(set(keys)) == 115
    assert set(keys) == expected
    assert f"turn:{new_turn.turn_ref}" not in keys
    assert f"turn:{new_turn.turn_ref}" in {item["key"] for item in await _all(service, source)}


@pytest.mark.asyncio
async def test_invalid_candidates_do_not_silently_cap_older_valid_activity(activity_db):
    factory, service = activity_db
    async with factory() as db:
        scope = await _scope(db)
        source = await _session(db, scope)
        direct = await _turn(db, source, at=AT - timedelta(hours=1))
        for number in range(205):
            db.add(
                CompanionReview(
                    lease_token=f"unrecorded-{number:04d}",
                    scope_id=scope.id,
                    persona_key="alpha",
                    mode_revision=1,
                    context_revision=1,
                    life_version=1,
                    lease_deadline=AT,
                    budget_day="2026-10-20",
                    started_at=AT,
                )
            )
        await db.commit()
    first = await service.list_activity(source.session_ref)
    assert first["items"] == []
    assert first["next_cursor"]
    items = await _all(service, source)
    assert [item["key"] for item in items] == [f"turn:{direct.turn_ref}"]


@pytest.mark.asyncio
async def test_api_validates_activity_view_and_filters_ordinary_sessions_before_limit(activity_db, monkeypatch):
    factory, service = activity_db
    async with factory() as db:
        scope = await _scope(db)
        source = await _session(db, scope)
        background = await _session(db, scope, purpose="companion_reflection")
        invalid_chat = await _session(db, scope, task="not-ordinary")
        await db.commit()

    async def summary(item):
        return {"session_ref": item.session_ref, "purpose": item.purpose}

    monkeypatch.setattr(AgentAdminService, "_session_summary", staticmethod(summary))
    app = FastAPI()
    app.include_router(create_agent_sessions_router(service, asset_dir=Path("plugins/llm_chat/webui_sessions")))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        path = f"{PREFIX}/scopes/{scope.scope_ref}/sessions"
        ordinary = await client.get(path, params={"purpose": "chat", "limit": 1})
        assert ordinary.json()["items"] == [{"session_ref": source.session_ref, "purpose": "chat"}]
        native = await client.get(path, params={"limit": 1})
        assert native.json()["items"][0]["session_ref"] == invalid_chat.session_ref
        assert (await client.get(path, params={"purpose": "unknown"})).status_code == 400
        turns = f"{PREFIX}/sessions/{source.session_ref}/turns"
        assert (await client.get(turns, params={"view": "unknown"})).status_code == 400
        assert (await client.get(turns, params={"view": "activity", "limit": 101})).status_code == 400
        assert (await client.get(turns, params={"view": "activity", "cursor": "x" * 1501})).status_code == 422
        for session in (background, invalid_chat):
            response = await client.get(f"{PREFIX}/sessions/{session.session_ref}/turns", params={"view": "activity"})
            assert response.status_code == 400
            assert response.json()["code"] == "invalid_activity_session"
        missing = await client.get(f"{PREFIX}/sessions/session_missing/turns", params={"view": "activity"})
        assert missing.status_code == 404


@pytest.mark.asyncio
async def test_occurrence_rejects_foreign_account_and_ambiguous_native_turn(activity_db):
    factory, service = activity_db
    async with factory() as db:
        scope = await _scope(db)
        foreign = await _scope(db, "source", account="other-bot")
        source = await _session(db, scope)
        creation = await _turn(db, source)
        task = await _task(db, scope, creation, "owned-task")
        native_session = await _session(db, scope, purpose="reminder", task=task.task_ref)
        native = await _turn(db, native_session, trigger="scheduled")
        foreign_session = await _session(db, foreign, purpose="reminder", task=task.task_ref)
        foreign_turn = await _turn(db, foreign_session, trigger="scheduled")
        db.add_all(
            [
                ReminderOccurrence(task_ref=task.task_ref, sequence=1, turn_ref=native.turn_ref, scheduled_at=AT),
                ReminderOccurrence(task_ref=task.task_ref, sequence=2, turn_ref=native.turn_ref, scheduled_at=AT),
                ReminderOccurrence(task_ref=task.task_ref, sequence=3, turn_ref=foreign_turn.turn_ref, scheduled_at=AT),
            ]
        )
        await db.commit()
    items = await _all(service, source, limit=1)
    occurrences = [item for item in items if item["kind"] == "reminder_occurrence"]
    assert len(occurrences) == 3
    assert all(item["recording_status"] == "invalid" for item in occurrences)
    assert all(item["occurrence"]["turn_ref"] is None for item in occurrences)
    assert not any(item.get("relation") == "reminder" for item in items)
