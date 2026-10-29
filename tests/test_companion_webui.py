"""Consumer-visible companion WebUI contracts using real isolated SQLite persistence."""

from __future__ import annotations

import json
from types import SimpleNamespace
from pathlib import Path
from datetime import UTC, datetime, timezone, timedelta

import httpx
import pytest
from fastapi import FastAPI, Request, HTTPException
import pytest_asyncio
from arclet.entari.config import EntariConfig

if not hasattr(EntariConfig, "instance"):
    EntariConfig.instance = EntariConfig.load(Path(__file__).resolve().parents[1] / "entari.full.example.yml")

from sqlalchemy import func, select
from entari_plugin_database import Base
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from plugins.llm_chat import agent_admin, personality, agent_events, session_manager, session_inspection
from plugins.llm_chat.config import LLMChatConfig
from plugins.llm_chat.models import (
    AgentTurn,
    ChatScope,
    AgentEvent,
    ContextSession,
    ReminderPreference,
    ScopePersonaSelection,
    SessionPersonaSnapshot,
)
from plugins.llm_chat.agent_admin import AgentAdminService
from plugins.llm_chat.agent_webui_api import create_agent_sessions_router
from plugins.llm_chat.companion.models import (
    CompanionLife,
    CompanionMode,
    CompanionMember,
    CompanionReview,
    CompanionContact,
    CompanionAttention,
    CompanionChatEvidence,
)

PREFIX = "/api/llm-chat/sessions"
LIFE_BEFORE = {"focus": "Fictional morning", "interests": [], "plans": [], "recent_events": []}
LIFE_AFTER = {"focus": "Fictional afternoon", "interests": ["colors"], "plans": [], "recent_events": []}


def _event(turn_id, sequence, kind, payload, *, status="recorded", visible=False, duration=0):
    return AgentEvent(
        turn_id=turn_id,
        sequence=sequence,
        event_type=kind,
        status=status,
        model_visible=visible,
        payload_json=json.dumps(payload),
        duration_ms=duration,
        attempt=1,
    )


async def _review(
    db,
    scope,
    moment,
    *,
    number,
    status="completed",
    contact=True,
    audit=True,
    decision="blocked",
    proposal=True,
    persona="alpha",
):
    ref = f"review_{number:03d}"
    started = moment - timedelta(minutes=number)
    committed = status == "completed"
    contact_data = (
        {
            "member_ref": "member_alice",
            "key": f"contact-{number}",
            "purpose": "continue",
            "topic": "painting",
            "reason": "A shared topic",
            "evidence_turn_ids": [1],
        }
        if contact
        else None
    )
    generated = {
        "life": LIFE_AFTER,
        "attention": [],
        "contact": contact_data,
        "review_after_minutes": 60,
        "decision_summary": "A brief structured decision summary.",
    }
    row = CompanionReview(
        lease_token=ref,
        scope_id=scope.id,
        persona_key=persona,
        mode_revision=3,
        context_revision=7,
        life_version=1,
        lease_deadline=moment + timedelta(minutes=5),
        budget_day=moment.replace(tzinfo=UTC).astimezone(timezone(timedelta(hours=8))).date().isoformat(),
        status=status,
        reason="" if committed else "real_chat_feedback" if status == "cancelled" else "provider_failed",
        started_at=started,
        finished_at=started + timedelta(seconds=2),
        committed_life_version=2 if committed else None,
        contact_proposal_json=json.dumps(contact_data) if committed and contact_data else None,
    )
    db.add(row)
    turn = None
    if audit:
        context = ContextSession(
            scope_id=scope.id,
            sequence=100 + number,
            purpose="companion_reflection",
            model_name="baseline-not-a-recorded-provider",
            turn_count=1,
        )
        db.add(context)
        await db.flush()
        db.add(
            SessionPersonaSnapshot(
                session_id=context.id,
                snapshot_json=json.dumps(
                    {"key": persona, "name": "Recorded persona", "prompt": "Historical", "appearance": ""}
                ),
            )
        )
        turn = AgentTurn(
            session_id=context.id,
            sequence=1,
            user_id="",
            user_name="",
            trigger_kind="reflection",
            status="completed" if proposal else "failed",
            created_at=started,
            finished_at=started + timedelta(seconds=1),
        )
        db.add(turn)
        await db.flush()
        state = {
            "life": LIFE_BEFORE,
            "members": [
                {
                    "member_ref": "member_alice",
                    "display_name": "Historical Alice",
                    "known_evidence_turn_ids": [1],
                    "attention": [],
                    "episodes": [{"turn_id": 1, "user": "real conversation"}],
                }
            ],
            "recent_contacts": [],
        }
        events = [
            _event(
                turn.id,
                1,
                "companion_reflection",
                {"lease_token": ref, "mode_revision": 3, "context_revision": 7, "life_version": 1},
                status="started",
            ),
            _event(turn.id, 2, "companion_state", state),
            _event(
                turn.id,
                3,
                "context_snapshot",
                {"selection": {"purpose": "companion_reflection"}, "persona": {"name": "Historical"}},
            ),
            _event(
                turn.id,
                4,
                "model_request",
                {
                    "request_id": ref,
                    "model": "recorded-provider",
                    "messages": [{"role": "user", "content": "HTTP-body-not-for-list" * 10000}],
                    "capture_status": "complete",
                },
            ),
            _event(
                turn.id,
                5,
                "model_response",
                {
                    "request_id": ref,
                    "usage": {
                        "prompt_tokens": 12,
                        "completion_tokens": 3,
                        "prompt_tokens_details": {"cached_tokens": 4},
                    }
                    if proposal
                    else None,
                    "content": "HTTP-response-not-for-list" * 10000,
                },
                status="succeeded" if proposal else "failed",
                duration=1234,
            ),
            _event(turn.id, 6, "model_attempt", {"metrics": {"total_tokens": 99999}}),
        ]
        if proposal:
            events.append(_event(turn.id, 7, "companion_reflection", {"proposal": generated}, status="succeeded"))
        else:
            events.append(_event(turn.id, 7, "companion_reflection", {"error": "ProviderError"}, status="failed"))
        if decision:
            events.append(
                _event(
                    turn.id,
                    8,
                    "companion_contact_decision",
                    {
                        "review_ref": ref,
                        "status": decision,
                        "reason": "no_contact_proposed"
                        if decision == "not_proposed"
                        else "contact_reserved"
                        if decision == "reserved"
                        else "daily_contact_limit"
                        if decision == "blocked"
                        else "reflection_failed",
                        "checked_at": moment.replace(tzinfo=UTC).isoformat(),
                        "facts": {"daily_contact_limit": 0},
                    },
                    status=decision,
                )
            )
        db.add_all(events)
    await db.flush()
    return row, turn


@pytest_asyncio.fixture
async def companion_webui(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'webui-companion.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    for module in (agent_admin, agent_events, personality, session_manager, session_inspection):
        monkeypatch.setattr(module, "get_session", factory)

    def forbidden_model(*args, **kwargs):
        raise AssertionError("Read-only companion inspection must not resolve or call a current model")

    monkeypatch.setattr(agent_admin, "get_model_config", forbidden_model)
    moment = datetime.now(UTC).replace(tzinfo=None)
    async with factory() as db, db.begin():
        scope = ChatScope(
            platform="test",
            account_id="secret-platform-account",
            channel_id="secret-platform-channel",
            display_name="Observed scope",
        )
        other = ChatScope(
            platform="test", account_id="other-account", channel_id="other-channel", display_name="Other scope"
        )
        db.add_all([scope, other])
        await db.flush()
        chat = ContextSession(scope_id=scope.id, sequence=1, purpose="chat")
        other_chat = ContextSession(scope_id=other.id, sequence=1, purpose="chat")
        autonomous = ContextSession(scope_id=scope.id, sequence=2, purpose="companion")
        db.add_all([chat, other_chat, autonomous])
        await db.flush()
        alice = AgentTurn(
            id=1,
            session_id=chat.id,
            sequence=1,
            user_id="unified-alice",
            user_name="Alice",
            status="completed",
            finished_at=moment,
        )
        bob = AgentTurn(
            id=2,
            session_id=chat.id,
            sequence=2,
            user_id="unified-bob",
            user_name="Bob",
            status="completed",
            finished_at=moment,
        )
        outsider = AgentTurn(
            id=3,
            session_id=other_chat.id,
            sequence=1,
            user_id="unified-alice",
            user_name="Other Alice",
            status="completed",
            finished_at=moment,
        )
        contact_turn = AgentTurn(
            id=4,
            session_id=autonomous.id,
            sequence=1,
            user_id="unified-alice",
            user_name="Alice",
            trigger_kind="autonomous",
            status="failed",
            finished_at=moment,
        )
        db.add_all([alice, bob, outsider, contact_turn])
        db.add_all(
            [
                ScopePersonaSelection(scope_id=scope.id, persona_key="alpha"),
                CompanionMode(
                    scope_id=scope.id,
                    mode="normal",
                    persona_key="alpha",
                    revision=99,
                    context_revision=101,
                    updated_at=moment,
                ),
                CompanionLife(
                    scope_id=scope.id,
                    persona_key="alpha",
                    version=99,
                    state_json=json.dumps({**LIFE_AFTER, "focus": "CURRENT ONLY"}),
                    updated_at=moment,
                ),
                CompanionLife(
                    scope_id=scope.id,
                    persona_key="retired",
                    version=1,
                    state_json=json.dumps(LIFE_BEFORE),
                    updated_at=moment,
                ),
                CompanionMember(
                    scope_id=scope.id,
                    persona_key="alpha",
                    member_ref="member_alice",
                    user_id="unified-alice",
                    platform_user_id="secret-platform-alice",
                    display_name="Current Alice",
                    version=5,
                    last_turn_id=1,
                    last_user_at=moment,
                    last_contact_at=moment,
                ),
                CompanionMember(
                    scope_id=scope.id,
                    persona_key="alpha",
                    member_ref="member_bob",
                    user_id="unified-bob",
                    platform_user_id="secret-platform-bob",
                    display_name="Current Bob",
                    version=3,
                    last_turn_id=2,
                    last_user_at=moment,
                ),
                CompanionChatEvidence(
                    scope_id=scope.id, persona_key="alpha", turn_id=1, member_ref="member_alice", received_at=moment
                ),
                CompanionChatEvidence(
                    scope_id=scope.id, persona_key="alpha", turn_id=2, member_ref="member_bob", received_at=moment
                ),
                CompanionChatEvidence(
                    scope_id=scope.id, persona_key="alpha", turn_id=3, member_ref="member_alice", received_at=moment
                ),
                ReminderPreference(account_id=scope.account_id, unified_user_id="unified-alice", enabled=False),
                ReminderPreference(account_id="other-account", unified_user_id="unified-bob", enabled=False),
                CompanionAttention(
                    scope_id=scope.id,
                    persona_key="alpha",
                    member_ref="member_alice",
                    key="topic",
                    state_json=json.dumps(
                        {
                            "member_ref": "member_alice",
                            "key": "topic",
                            "kind": "topic",
                            "summary": "Actual stored attention",
                            "status": "waiting_reply",
                            "evidence_turn_ids": [1],
                        }
                    ),
                    version=3,
                    updated_at=moment,
                ),
            ]
        )
        reviews = []
        for number, values in enumerate(
            [
                {"decision": "reserved"},
                {"contact": False, "decision": "not_proposed"},
                {"decision": "blocked"},
                {"status": "failed", "proposal": False, "decision": "failed"},
                {"status": "cancelled", "decision": None},
                {"audit": False, "persona": "retired"},
            ],
            1,
        ):
            reviews.append(await _review(db, scope, moment, number=number, **values))
        foreign_review, _ = await _review(db, other, moment, number=50, audit=False)
        db.add(
            CompanionContact(
                attempt_ref="attempt_actual_unknown",
                scope_id=scope.id,
                persona_key="alpha",
                mode_revision=3,
                context_revision=7,
                origin_review_token=reviews[0][0].lease_token,
                member_ref="member_alice",
                member_version=4,
                user_id="unified-alice",
                platform_user_id="secret-platform-alice",
                display_name="Attempt Alice",
                proposal_key="contact-1",
                identity="private-contact-identity",
                proposal_json=reviews[0][0].contact_proposal_json,
                limits_json=json.dumps({"daily_contacts": 1}),
                budget_day=moment.replace(tzinfo=UTC).astimezone(timezone(timedelta(hours=8))).date().isoformat(),
                lease_token="secret-contact-lease",
                lease_deadline=moment + timedelta(hours=1),
                turn_id=4,
                status="unknown",
                reason="transport_timeout",
                transport_started_at=moment,
                transport_inflight=True,
                confirmed_receipts_json=json.dumps(["secret-receipt"]),
                created_at=moment,
                finished_at=moment,
            )
        )
        db.add_all(
            [
                _event(contact_turn.id, 1, "conversation_mode", {"mode": "companion", "revision": 3}),
                _event(
                    contact_turn.id,
                    2,
                    "companion_trigger",
                    {"purpose": "continue", "topic": "painting", "reason": "shared topic"},
                ),
                _event(
                    contact_turn.id,
                    3,
                    "response_decision",
                    {"should_reply": True, "reason": "Shared topic", "outcome": "reply"},
                ),
                AgentEvent(
                    turn_id=contact_turn.id,
                    sequence=4,
                    event_type="message_delivery",
                    status="confirmed",
                    effect="confirmed",
                    model_visible=False,
                    payload_json=json.dumps({"content": "Confirmed prefix"}),
                ),
            ]
        )
    service = AgentAdminService(LLMChatConfig(companion_daily_contacts=24), [])
    app = FastAPI()

    async def authenticated(request: Request):
        if request.headers.get("x-session") != "authenticated":
            raise HTTPException(status_code=401, detail="WebUI login required")

    app.include_router(
        create_agent_sessions_router(
            service,
            asset_dir=Path(__file__).resolve().parents[1] / "plugins/llm_chat/webui_sessions",
            auth_dependency=authenticated,
        )
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"x-session": "authenticated"}
        ) as client:
            yield SimpleNamespace(
                client=client,
                factory=factory,
                scope=scope,
                other=other,
                reviews=reviews,
                foreign_review=foreign_review,
                service=service,
                contact_turn=contact_turn,
                moment=moment,
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_companion_overview_exposes_only_current_state_actual_permissions_and_daily_counts(companion_webui):
    data = companion_webui
    response = await data.client.get(f"{PREFIX}/scopes/{data.scope.scope_ref}/companion")
    assert response.status_code == 200
    assert "no-store" in response.headers["cache-control"]
    item = response.json()["item"]
    assert item["current"] is True
    assert item["mode"]["mode"] == "normal"
    assert item["mode"]["revision"] == 99
    assert item["life"]["state"]["focus"] == "CURRENT ONLY"
    assert item["current_limits"]["daily_contacts"] == 24
    assert item["daily"]["reviews"] == {"completed": 4, "failed": 1, "cancelled": 1}
    assert item["daily"]["contacts"] == {"unknown": 1}
    assert item["daily"]["confirmed_messages"] == 1
    members = {item["member_ref"]: item for item in item["members"]}
    assert members["member_alice"]["preference"]["enabled"] is False
    assert members["member_alice"]["preference"]["recorded"] is True
    assert members["member_alice"]["attention"][0]["state"]["status"] == "waiting_reply"
    assert members["member_bob"]["preference"] == {
        "enabled": True,
        "recorded": False,
        "source": "default_allowed",
        "updated_at": None,
    }
    assert item["personas"] == ["alpha", "retired"]
    assert "secret-platform" not in response.text
    unknown = await data.client.get(f"{PREFIX}/scopes/{data.other.scope_ref}/companion")
    assert unknown.json()["item"]["mode"]["recorded"] is False
    assert unknown.json()["item"]["mode"]["persona_key"] == ""
    assert unknown.json()["item"]["life"] is None
    assert unknown.json()["item"]["members"] == []


@pytest.mark.asyncio
async def test_review_history_keeps_generated_host_and_actual_delivery_facts_separate(companion_webui):
    data = companion_webui
    response = await data.client.get(f"{PREFIX}/scopes/{data.scope.scope_ref}/companion/reviews")
    assert response.status_code == 200
    items = {item["review_ref"]: item for item in response.json()["items"]}
    reserved = items["review_001"]
    assert reserved["contact_proposed"] is True
    assert reserved["contact"]["display_name"] == "Historical Alice"
    assert reserved["decision"]["status"] == "reserved"
    assert reserved["decision"]["source"] == "companion_contact_decision"
    assert reserved["contacts"][0]["status"] == "unknown"
    assert reserved["contacts"][0]["confirmed_count"] == 1
    assert reserved["contacts"][0]["target"] == "member"
    assert reserved["contacts"][0]["native_turn"]["turn_ref"] == data.contact_turn.turn_ref
    assert reserved["model"] == "recorded-provider"
    assert reserved["usage"]["total_tokens"] == 15
    assert reserved["usage"]["cached_input_tokens"] == 4
    assert reserved["usage"]["measured_requests"] == 1
    assert reserved["usage"]["coverage"]["complete"] is True
    assert reserved["model_calls"][0]["duration_ms"] == 1234
    assert items["review_002"]["contact_proposed"] is False
    assert items["review_002"]["decision"]["reason"] == "no_contact_proposed"
    assert items["review_003"]["decision"]["reason"] == "daily_contact_limit"
    assert items["review_003"]["contacts"] == []
    assert items["review_004"]["contact_proposed"] is None
    assert items["review_004"]["usage"]["total_tokens"] is None
    assert items["review_004"]["usage"]["unknown_requests"] == 1
    assert items["review_005"]["contact_proposed"] is True
    assert items["review_005"]["committed_contact"] is None
    assert items["review_005"]["decision"] is None
    legacy = items["review_006"]
    assert legacy["native_turn"] is None
    assert legacy["model"] is None
    assert legacy["usage"]["source"] == "not_recorded"
    assert legacy["usage"]["total_tokens"] is None
    assert legacy["contact_proposed"] is None
    assert legacy["decision"] is None
    assert "HTTP-body-not-for-list" not in response.text
    assert "HTTP-response-not-for-list" not in response.text
    assert "secret-receipt" not in response.text
    assert "secret-contact-lease" not in response.text
    assert "secret-platform" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["channel", ["invalid"]])
async def test_channel_contact_is_not_presented_as_an_unknown_member(companion_webui, target):
    data = companion_webui
    async with data.factory() as db, db.begin():
        contact = await db.get(CompanionContact, "attempt_actual_unknown")
        proposal = json.loads(contact.proposal_json)
        proposal.update(target=target, member_ref=None, purpose="share", evidence_turn_ids=[])
        contact.proposal_json = json.dumps(proposal)
        contact.member_ref = contact.user_id = contact.platform_user_id = contact.display_name = ""
        review_row = await db.get(CompanionReview, data.reviews[0][0].lease_token)
        review_row.contact_proposal_json = json.dumps(proposal)
        event = await db.scalar(
            select(AgentEvent).where(
                AgentEvent.turn_id == data.reviews[0][1].id,
                AgentEvent.event_type == "companion_reflection",
                AgentEvent.status == "succeeded",
            )
        )
        payload = json.loads(event.payload_json)
        payload["proposal"]["contact"] = proposal
        event.payload_json = json.dumps(payload)
    response = await data.client.get(f"{PREFIX}/scopes/{data.scope.scope_ref}/companion/reviews")
    assert response.status_code == 200
    review = next(item for item in response.json()["items"] if item["review_ref"] == "review_001")
    assert review["contact"]["target"] == ("channel" if target == "channel" else None)
    assert review["contact"]["display_name"] is None
    assert review["committed_contact"]["target"] == ("channel" if target == "channel" else None)
    contact = review["contacts"][0]
    assert contact["target"] == ("channel" if target == "channel" else None)
    assert contact["display_name"] == ""
    assert contact["confirmed_count"] == 1


@pytest.mark.asyncio
async def test_review_cursor_filters_are_deterministic_scoped_and_bounded(companion_webui):
    data = companion_webui
    path = f"{PREFIX}/scopes/{data.scope.scope_ref}/companion/reviews"
    first = (await data.client.get(path, params={"limit": 2, "persona_key": "alpha"})).json()
    assert [item["review_ref"] for item in first["items"]] == ["review_001", "review_002"]
    second = (
        await data.client.get(path, params={"limit": 2, "persona_key": "alpha", "cursor": first["next_cursor"]})
    ).json()
    assert [item["review_ref"] for item in second["items"]] == ["review_003", "review_004"]
    third = (
        await data.client.get(path, params={"limit": 2, "persona_key": "alpha", "cursor": second["next_cursor"]})
    ).json()
    assert [item["review_ref"] for item in third["items"]] == ["review_005"]
    assert third["next_cursor"] is None
    only_cancelled = (await data.client.get(path, params={"status": "cancelled"})).json()
    assert [item["review_ref"] for item in only_cancelled["items"]] == ["review_005"]
    retired = (await data.client.get(path, params={"persona_key": "retired"})).json()
    assert [item["review_ref"] for item in retired["items"]] == ["review_006"]
    assert (await data.client.get(path, params={"status": "quiet_hours"})).status_code == 400
    assert (await data.client.get(path, params={"limit": 101})).status_code == 422
    assert (await data.client.get(path, params={"persona_key": "../alpha"})).status_code == 400
    assert (await data.client.get(path, params={"cursor": "not-a-cursor"})).status_code == 400
    assert (await data.client.get(path, params={"cursor": first["next_cursor"]})).status_code == 400
    assert (
        await data.client.get(
            f"{PREFIX}/scopes/{data.other.scope_ref}/companion/reviews",
            params={"cursor": first["next_cursor"], "persona_key": "alpha"},
        )
    ).status_code == 400
    missing = await data.client.get(f"{PREFIX}/scopes/missing/companion/reviews")
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_review_detail_uses_native_input_proposal_and_real_commit_boundary(companion_webui):
    data = companion_webui
    base = f"{PREFIX}/scopes/{data.scope.scope_ref}/companion/reviews"
    detail = (await data.client.get(base + "/review_001")).json()["item"]
    assert detail["historical"] is True
    assert detail["state"]["life"] == LIFE_BEFORE
    assert detail["proposal"]["life"] == LIFE_AFTER
    assert detail["proposal"]["decision_summary"] == "A brief structured decision summary."
    assert detail["changes"]["life_before"] == LIFE_BEFORE
    assert detail["changes"]["life_proposed"] == LIFE_AFTER
    assert detail["changes"]["committed"] is True
    assert detail["capture"]["state"] == "recorded"
    assert detail["capture"]["proposal"] == "recorded"
    assert detail["events"]["state_event_ref"]
    assert detail["events"]["proposal_event_ref"]
    assert detail["contacts"][0]["limits"] == {"daily_contacts": 1}
    assert detail["contacts"][0]["confirmed_count"] == 1
    assert detail["evidence_turns"][0]["turn_id"] == 1
    assert detail["evidence_turns"][0]["member_ref"] == "member_alice"
    event_ref = detail["events"]["proposal_event_ref"]
    payload = await data.client.get(f"{PREFIX}/events/{event_ref}/payload", params={"path": "proposal", "limit": 256})
    assert payload.status_code == 200
    assert payload.json()["item"]["next_offset"] is not None
    cancelled = (await data.client.get(base + "/review_005")).json()["item"]
    assert cancelled["proposal"]["contact"] is not None
    assert cancelled["changes"]["committed"] is False
    assert cancelled["committed_life_version"] is None
    assert cancelled["decision"] is None
    failed = (await data.client.get(base + "/review_004")).json()["item"]
    assert failed["proposal"] is None
    assert failed["changes"]["committed"] is False
    historical = (await data.client.get(base + "/review_006")).json()["item"]
    assert historical["state"] is None
    assert historical["proposal"] is None
    assert historical["decision"] is None
    assert historical["changes"]["committed"] is False
    assert historical["evidence_turns"] == []
    assert "current_limits" not in historical
    assert (
        await data.client.get(f"{PREFIX}/scopes/{data.other.scope_ref}/companion/reviews/review_001")
    ).status_code == 404
    assert (await data.client.get(base + "/missing")).status_code == 404


@pytest.mark.asyncio
async def test_reflection_inspection_reuses_authoritative_historical_review(companion_webui):
    data = companion_webui
    turn = data.reviews[0][1]
    response = await data.client.get(f"{PREFIX}/turns/{turn.turn_ref}/inspection")
    assert response.status_code == 200
    inspection = response.json()["item"]
    review = inspection["companion"]["review"]
    detail = (await data.client.get(f"{PREFIX}/scopes/{data.scope.scope_ref}/companion/reviews/review_001")).json()[
        "item"
    ]
    assert review == detail
    assert review["native_turn"]["turn_ref"] == turn.turn_ref
    assert review["persona_key"] == inspection["persona"]["key"] == "alpha"
    assert review["historical"] is True
    assert review["proposal"]["decision_summary"] == "A brief structured decision summary."
    assert review["state"]["life"] == LIFE_BEFORE
    assert review["proposal"]["life"] == LIFE_AFTER
    assert review["changes"]["committed"] is True
    assert review["transaction_committed"] is True
    assert review["committed_life_version"] == 2
    assert review["decision"]["status"] == "reserved"
    assert review["decision"]["reason"] == "contact_reserved"
    assert review["contacts"][0]["confirmed_count"] == 1
    assert review["contacts"][0]["status"] == "unknown"
    assert review["contacts"][0]["limits"] == {"daily_contacts": 1}
    assert [item["turn_id"] for item in review["evidence_turns"]] == [1]
    assert inspection["companion"]["reflection"]["proposal"] == review["proposal"]
    serialized = json.dumps(review)
    for private in (
        "CURRENT ONLY",
        "lease_token",
        "lease_deadline",
        "secret-platform",
        "secret-contact-lease",
        "secret-receipt",
        "private-contact-identity",
        "HTTP-body-not-for-list",
        "HTTP-response-not-for-list",
    ):
        assert private not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("index", "status", "committed", "gate"),
    [
        (1, "completed", True, "not_proposed"),
        (2, "completed", True, "blocked"),
        (3, "failed", False, "failed"),
        (4, "cancelled", False, None),
    ],
)
async def test_reflection_inspection_preserves_actual_review_outcomes(companion_webui, index, status, committed, gate):
    data = companion_webui
    turn = data.reviews[index][1]
    result = (await data.client.get(f"{PREFIX}/turns/{turn.turn_ref}/inspection")).json()["item"]
    review = result["companion"]["review"]
    assert review["review_ref"] == data.reviews[index][0].lease_token
    assert review["status"] == status
    assert review["changes"]["committed"] is committed
    assert (review["decision"]["status"] if review["decision"] else None) == gate
    assert (review["proposal"] is not None) is (status != "failed")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("purpose", "trigger"),
    [("chat", "message"), ("chat", "reflection"), ("companion_reflection", "message"), ("companion", "autonomous")],
)
async def test_nonreflection_inspection_and_turn_lists_skip_review_lookup(companion_webui, monkeypatch, purpose, trigger):
    data = companion_webui
    turn = data.reviews[0][1]
    async with data.factory() as db, db.begin():
        stored_turn = await db.get(AgentTurn, turn.id)
        context = await db.get(ContextSession, turn.session_id)
        stored_turn.trigger_kind = trigger
        context.purpose = purpose
        session_ref = context.session_ref

    def forbidden_review(*args, **kwargs):
        raise AssertionError("Only native reflection inspection may look up a review")

    monkeypatch.setattr(agent_admin, "companion_review_detail", forbidden_review)
    result = (await data.client.get(f"{PREFIX}/turns/{turn.turn_ref}/inspection")).json()["item"]
    assert result["companion"]["review"] is None
    assert result["companion"]["reflection"]["proposal"]["life"] == LIFE_AFTER
    turns = await data.client.get(f"{PREFIX}/sessions/{session_ref}/turns")
    assert turns.status_code == 200
    assert len(turns.json()["items"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ownership",
    [
        "missing_start",
        "public_start",
        "invalid_ref",
        "missing_review",
        "foreign_review",
        "wrong_scope",
        "wrong_persona",
        "missing_persona",
        "unkeyed_persona",
        "duplicate_start",
        "conflicting_start",
        "duplicate_owner",
    ],
)
async def test_reflection_inspection_rejects_missing_or_ambiguous_review_ownership(companion_webui, ownership):
    data = companion_webui
    turn = data.reviews[0][1]
    async with data.factory() as db, db.begin():
        start = await db.scalar(
            select(AgentEvent).where(
                AgentEvent.turn_id == turn.id,
                AgentEvent.event_type == "companion_reflection",
                AgentEvent.status == "started",
            )
        )
        if ownership == "missing_start":
            await db.delete(start)
        elif ownership == "public_start":
            start.model_visible = True
        elif ownership in {"invalid_ref", "missing_review", "foreign_review"}:
            start.payload_json = json.dumps(
                {
                    "lease_token": []
                    if ownership == "invalid_ref"
                    else "missing-review"
                    if ownership == "missing_review"
                    else data.foreign_review.lease_token
                }
            )
        elif ownership == "wrong_scope":
            context = await db.get(ContextSession, turn.session_id)
            context.scope_id = data.other.id
        elif ownership == "wrong_persona":
            review = await db.get(CompanionReview, "review_001")
            review.persona_key = "retired"
        elif ownership in {"missing_persona", "unkeyed_persona"}:
            snapshot = await db.get(SessionPersonaSnapshot, turn.session_id)
            if ownership == "missing_persona":
                await db.delete(snapshot)
            else:
                snapshot.snapshot_json = json.dumps({"name": "Historical without an identity"})
        elif ownership in {"duplicate_start", "conflicting_start"}:
            db.add(
                _event(
                    turn.id,
                    9,
                    "companion_reflection",
                    {"lease_token": "review_001" if ownership == "duplicate_start" else "review_002"},
                    status="started",
                )
            )
        elif ownership == "duplicate_owner":
            competing_start = await db.scalar(
                select(AgentEvent).where(
                    AgentEvent.turn_id == data.reviews[1][1].id,
                    AgentEvent.event_type == "companion_reflection",
                    AgentEvent.status == "started",
                )
            )
            competing_start.payload_json = start.payload_json
    response = await data.client.get(f"{PREFIX}/turns/{turn.turn_ref}/inspection")
    assert response.status_code == 200
    companion = response.json()["item"]["companion"]
    assert companion["review"] is None
    assert companion["reflection"]["historical"] is True
    assert companion["reflection"]["proposal"]["life"] == LIFE_AFTER
    if ownership == "duplicate_owner":
        competing = await data.client.get(f"{PREFIX}/turns/{data.reviews[1][1].turn_ref}/inspection")
        assert competing.status_code == 200
        assert competing.json()["item"]["companion"]["review"] is None


@pytest.mark.asyncio
async def test_evidence_jumps_cannot_associate_other_scope_persona_or_member(companion_webui):
    data = companion_webui
    reflection_turn = data.reviews[0][1]
    async with data.factory() as db, db.begin():
        event = await db.scalar(
            select(AgentEvent).where(AgentEvent.turn_id == reflection_turn.id, AgentEvent.sequence == 7)
        )
        payload = json.loads(event.payload_json)
        payload["proposal"]["attention"] = [
            {"member_ref": "member_alice", "key": "forged", "evidence_turn_ids": [2, 3]}
        ]
        event.payload_json = json.dumps(payload)
        # A wrong-scope native ID on the actual contact must not become a navigation link.
        attempt = await db.get(CompanionContact, "attempt_actual_unknown")
        attempt.turn_id = 3
    detail = (await data.client.get(f"{PREFIX}/scopes/{data.scope.scope_ref}/companion/reviews/review_001")).json()[
        "item"
    ]
    assert [item["turn_id"] for item in detail["evidence_turns"]] == [1]
    assert detail["contacts"][0]["native_turn"] is None
    async with data.factory() as db, db.begin():
        evidence = await db.get(CompanionChatEvidence, (data.scope.id, "alpha", 1, "member_alice"))
        evidence.persona_key = "retired"
    detail = (await data.client.get(f"{PREFIX}/scopes/{data.scope.scope_ref}/companion/reviews/review_001")).json()[
        "item"
    ]
    assert detail["evidence_turns"] == []


@pytest.mark.asyncio
async def test_readonly_auth_turn_capture_and_internal_rollover_boundary(companion_webui):
    data = companion_webui
    paths = [
        f"{PREFIX}/scopes/{data.scope.scope_ref}/companion",
        f"{PREFIX}/scopes/{data.scope.scope_ref}/companion/reviews",
        f"{PREFIX}/scopes/{data.scope.scope_ref}/companion/reviews/review_001",
        f"{PREFIX}/turns/{data.reviews[0][1].turn_ref}/inspection",
    ]
    for path in paths:
        unauthenticated = await data.client.get(path, headers={"x-session": ""})
        assert unauthenticated.status_code == 401
        assert "no-store" in unauthenticated.headers["cache-control"]
    async with data.factory() as db:
        before = {
            model.__tablename__: await db.scalar(select(func.count()).select_from(model))
            for model in (CompanionMode, CompanionReview, CompanionContact, CompanionLife, AgentTurn, AgentEvent)
        }
    reflection = (await data.client.get(paths[-1])).json()["item"]
    assert reflection["turn"]["purpose"] == "companion_reflection"
    assert reflection["turn"]["trigger_kind"] == "reflection"
    assert reflection["companion"]["reflection"]["proposal"]["life"] == LIFE_AFTER
    assert reflection["companion"]["mode"] is None
    contact = (await data.client.get(f"{PREFIX}/turns/{data.contact_turn.turn_ref}/inspection")).json()["item"]
    assert contact["turn"]["purpose"] == "companion"
    assert contact["companion"]["mode"]["mode"] == "companion"
    assert contact["companion"]["trigger"]["purpose"] == "continue"
    assert contact["response_decision"] is not None
    for path in paths[:3]:
        assert (await data.client.get(path)).status_code == 200
        assert (await data.client.post(path, json={})).status_code == 405
    native = (await data.client.get(paths[2])).json()["item"]["native_turn"]
    sessions = (await data.client.get(f"{PREFIX}/scopes/{data.scope.scope_ref}/sessions")).json()["items"]
    assert {item["purpose"] for item in sessions} == {"chat", "companion", "companion_reflection"}
    session_detail = (await data.client.get(f"{PREFIX}/sessions/{native['session_ref']}")).json()["item"]
    assert session_detail["purpose"] == "companion_reflection"
    assert session_detail["persona"]["name"] == "Recorded persona"
    internal_turns = (await data.client.get(f"{PREFIX}/sessions/{native['session_ref']}/turns")).json()["items"]
    assert [item["trigger_kind"] for item in internal_turns] == ["reflection"]
    mutation = await data.client.post(
        f"{PREFIX}/scopes/{data.scope.scope_ref}/sessions/{native['session_ref']}/rollover",
        json={"carry_handoff": False},
    )
    assert mutation.status_code == 409
    assert mutation.json()["code"] == "unsupported_purpose"
    async with data.factory() as db:
        after = {
            model.__tablename__: await db.scalar(select(func.count()).select_from(model))
            for model in (CompanionMode, CompanionReview, CompanionContact, CompanionLife, AgentTurn, AgentEvent)
        }
    assert before == after


@pytest.mark.asyncio
async def test_usage_is_per_request_and_missing_metrics_stay_unknown(companion_webui):
    data = companion_webui
    turn = data.reviews[0][1]
    async with data.factory() as db, db.begin():
        db.add(
            _event(
                turn.id,
                9,
                "model_request",
                {"request_id": "second-request", "model": "second-model", "capture_status": "complete"},
            )
        )
    result = (await data.client.get(f"{PREFIX}/scopes/{data.scope.scope_ref}/companion/reviews/review_001")).json()[
        "item"
    ]
    assert result["model"] == "recorded-provider, second-model"
    assert result["usage"]["total_tokens"] == 15
    assert result["usage"]["measured_requests"] == 1
    assert result["usage"]["unknown_requests"] == 1
    assert result["usage"]["coverage"]["complete"] is False
    assert len(result["model_calls"]) == 2
    assert result["model_calls"][1]["usage"]["total_tokens"] is None
    assert result["model_calls"][1]["status"] == "not_recorded"


@pytest.mark.asyncio
async def test_unknown_receipt_capture_is_not_zero_and_unchanged_life_can_commit(companion_webui):
    data = companion_webui
    async with data.factory() as db, db.begin():
        attempt = await db.get(CompanionContact, "attempt_actual_unknown")
        attempt.confirmed_receipts_json = "invalid receipt audit"
        no_contact = await db.get(CompanionReview, "review_002")
        no_contact.life_version = no_contact.committed_life_version = 0
    overview = (await data.client.get(f"{PREFIX}/scopes/{data.scope.scope_ref}/companion")).json()["item"]
    assert overview["daily"]["confirmed_messages"] is None
    assert overview["daily"]["receipt_capture"] == "invalid"
    detail = (await data.client.get(f"{PREFIX}/scopes/{data.scope.scope_ref}/companion/reviews/review_001")).json()[
        "item"
    ]
    assert detail["contacts"][0]["confirmed_count"] is None
    assert detail["contacts"][0]["receipt_capture"] == "invalid"
    assert detail["contacts"][0]["status"] == "unknown"
    no_contact = (await data.client.get(f"{PREFIX}/scopes/{data.scope.scope_ref}/companion/reviews/review_002")).json()[
        "item"
    ]
    assert no_contact["changes"]["committed"] is True
    assert no_contact["committed_life_version"] == 0


@pytest.mark.asyncio
async def test_equal_timestamp_cursor_and_overflow_keep_original_evidence_refs(companion_webui):
    data = companion_webui
    async with data.factory() as db, db.begin():
        first = await db.get(CompanionReview, "review_001")
        second = await db.get(CompanionReview, "review_002")
        second.started_at = first.started_at
        event = await db.scalar(
            select(AgentEvent).where(AgentEvent.turn_id == data.reviews[0][1].id, AgentEvent.sequence == 2)
        )
        event.payload_json = json.dumps({"life": {"focus": "Large historical input" * 10000}, "members": []})
    path = f"{PREFIX}/scopes/{data.scope.scope_ref}/companion/reviews"
    first = (await data.client.get(path, params={"limit": 1})).json()
    assert first["items"][0]["review_ref"] == "review_002"
    second = (await data.client.get(path, params={"limit": 1, "cursor": first["next_cursor"]})).json()
    assert second["items"][0]["review_ref"] == "review_001"
    detail = (await data.client.get(path + "/review_001")).json()["item"]
    assert detail["state"] is None
    assert detail["capture"]["state"] == "overflow"
    assert detail["audit_truncated"] is True
    assert detail["events"]["state_event_ref"]
    payload = await data.client.get(
        f"{PREFIX}/events/{detail['events']['state_event_ref']}/payload", params={"limit": 256}
    )
    assert payload.status_code == 200
    assert payload.json()["item"]["next_offset"] == 256
