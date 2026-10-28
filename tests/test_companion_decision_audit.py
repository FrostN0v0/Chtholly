"""Host contact decisions stay distinct from proposals and external outcomes."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from companion_helpers import (
    NOW,
    Clock,
    scope,
    config,
    settled,
    response,
    native_rows,
    enable_member,
    install_provider,
)

from utils.companion_core import LifeState, ContactProposal, ReflectionProposal
from plugins.llm_chat.models import AgentEvent
from plugins.llm_chat.companion import store
from plugins.llm_chat.tools._delivery import send_with_delivery
from plugins.llm_chat.companion.models import CompanionReview, CompanionContact
from plugins.llm_chat.companion.service import CompanionService

pytest_plugins = ["companion_helpers"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("propose", "outcome", "cap", "status", "reason"),
    [
        (False, "success", 24, "not_proposed", "no_contact_proposed"),
        (True, "success", 0, "queued", "dialogue_opportunity_queued"),
        (True, "success", 24, "queued", "dialogue_opportunity_queued"),
        (True, "unknown", 24, "queued", "dialogue_opportunity_queued"),
        (True, "provider_error", 24, "failed", "TimeoutError"),
    ],
)
async def test_native_reflection_records_actual_gate_without_replaying(
    companion_db, native_transport, monkeypatch, propose, outcome, cap, status, reason
):
    native_scope = await scope(companion_db)
    member = await enable_member(companion_db, native_scope)
    clock = Clock()
    proposal = ReflectionProposal(
        life=LifeState(focus="An imagined painting afternoon"),
        contact=ContactProposal(
            member_ref=member.member_ref,
            key="colors",
            purpose="free_chat",
            topic="Talk about colors",
            reason="A shared interest in painting",
        )
        if propose
        else None,
    )
    scripted = (
        [TimeoutError("External provider timed out")]
        if outcome == "provider_error"
        else [response(proposal.model_dump_json())]
    )
    payloads = install_provider(monkeypatch, scripted)
    native_transport.network.outcome = "unknown" if outcome == "unknown" else "success"

    async def runner(session, _ctx, prepared, **_kwargs):
        await send_with_delivery(
            session, "The colors are ready.", prepared.delivery_state, texts=["The colors are ready."]
        )
        await prepared.lifecycle.persist_delivered_text()
        return "completed"

    worker = CompanionService(
        config(companion_daily_contacts=cap),
        accounts=lambda: [native_transport.account],
        clock=clock,
        runner=runner,
    )
    try:
        await worker.start()
        await settled(worker)
        events = await native_rows(companion_db, AgentEvent)
        audit = [event for event in events if event.event_type == "companion_contact_decision"]
        assert len(audit) == 1
        decision = json.loads(audit[0].payload_json)
        assert (decision["status"], decision["reason"]) == (status, reason)
        assert not audit[0].model_visible
        review = (await native_rows(companion_db, CompanionReview))[0]
        assert decision["review_ref"] == review.lease_token
        if cap == 0:
            assert review.status == "completed"
            assert review.contact_proposal_json is not None
        contacts = await native_rows(companion_db, CompanionContact)
        should_attempt = propose and cap > 0 and outcome != "provider_error"
        assert "attempt_ref" not in decision
        if should_attempt:
            assert len(contacts) == 1
            assert len(native_transport.network.wire) == 1
            assert contacts[0].origin_review_token == review.lease_token
            assert contacts[0].status == ("unknown" if outcome == "unknown" else "delivered")
        else:
            assert contacts == []
            assert native_transport.network.wire == []
        requests = len(payloads)
        await settled(worker)
        assert len(payloads) == requests
        assert len(native_transport.network.wire) == (1 if should_attempt else 0)
    finally:
        await worker.aclose()


@pytest.mark.asyncio
async def test_mode_revocation_after_generated_proposal_does_not_report_committed_or_sent(
    companion_db, native_transport, monkeypatch
):
    native_scope = await scope(companion_db)
    member = await enable_member(companion_db, native_scope)

    async def provider(_payload):
        await store.set_mode(native_scope.id, "normal", persona_key="alpha", now=NOW + timedelta(seconds=1))
        return response(
            ReflectionProposal(
                life=LifeState(focus="Uncommitted fictional focus"),
                contact=ContactProposal(
                    member_ref=member.member_ref,
                    key="colors",
                    purpose="free_chat",
                    topic="Colors",
                    reason="Shared interest",
                ),
            ).model_dump_json()
        )

    install_provider(monkeypatch, [provider])
    worker = CompanionService(config(), accounts=lambda: [native_transport.account], clock=Clock())
    try:
        await worker.start()
        await settled(worker)
        events = await native_rows(companion_db, AgentEvent)
        decisions = [
            json.loads(event.payload_json) for event in events if event.event_type == "companion_contact_decision"
        ]
        assert [(item["status"], item["reason"]) for item in decisions] == [("blocked", "mode_changed")]
        review = (await native_rows(companion_db, CompanionReview))[0]
        assert review.status == "cancelled"
        assert review.committed_life_version is None
        assert review.contact_proposal_json is None
        assert await native_rows(companion_db, CompanionContact) == []
        assert native_transport.network.wire == []
    finally:
        await worker.aclose()
