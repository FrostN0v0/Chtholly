"""Authenticated AgentEvent WebUI API contracts."""

from __future__ import annotations

import json
from types import SimpleNamespace
from pathlib import Path
from datetime import datetime, timezone, timedelta

import httpx
import pytest
from fastapi import FastAPI, Request, HTTPException
from arclet.entari.config import EntariConfig

if not hasattr(EntariConfig, "instance"):
    setattr(
        EntariConfig, "instance", EntariConfig.load(Path(__file__).resolve().parents[1] / "entari.full.example.yml")
    )

from entari_plugin_database import Base
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from plugins.llm_chat import (
    identity,
    agent_admin,
    personality,
    agent_events,
    session_manager,
    agent_event_view,
    session_inspection,
)
from plugins.llm_chat.config import LLMChatConfig
from plugins.llm_chat.models import AgentTurn, AgentEvent, RelationshipEvidence
from plugins.llm_chat.agent_admin import AgentAdminService
from plugins.llm_chat.agent_webui_api import create_agent_sessions_router
from plugins.llm_chat.session_manager import ScopeIdentity, BaselineFingerprint
from plugins.llm_chat.core.agent_trace import AgentTurnRecorder


@pytest.mark.asyncio
async def test_agent_sessions_api_exposes_timeline_context_payload_and_safe_reset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    for module in (agent_admin, agent_events, session_manager, session_inspection, personality):
        monkeypatch.setattr(module, "get_session", session_factory)
    monkeypatch.setattr(agent_admin, "get_model_config", lambda *_args: SimpleNamespace(name="test-model"))

    baseline = BaselineFingerprint("test-model", "persona", "system", "tools", "policy")
    scope = await session_manager.get_or_create_scope(ScopeIdentity("test", "bot", "guild", "channel", "Test Channel"))
    context_session = await session_manager.create_session(scope.id, baseline, start_reason="initial")
    turn = await session_manager.start_turn(
        context_session,
        trigger_message_id="message",
        user_id="alice",
        user_name="Alice",
        conversation_user_id=None,
        fresh_context=False,
    )
    attachment_ref = "input_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    attachment_bytes = b"private-user-image"
    (tmp_path / f"{attachment_ref}.png").write_bytes(attachment_bytes)
    reference_ref = "reference_cccccccccccccccccccccccccccccccc"
    output_ref = "output_dddddddddddddddddddddddddddddddd"
    reference_bytes = b"private-web-reference"
    output_bytes = b"private-edit-result"
    (tmp_path / f"{reference_ref}.png").write_bytes(reference_bytes)
    (tmp_path / f"{output_ref}.png").write_bytes(output_bytes)
    recorder = AgentTurnRecorder()
    recorder.record_user_input(
        "hello",
        user_name="Alice",
        fresh_context=False,
        attachments=[
            {
                "attachment_ref": attachment_ref,
                "mime": "image/png",
                "bytes": len(attachment_bytes),
                "source": "direct",
                "index": 1,
            }
        ],
    )
    recorder.append(
        "context_selection",
        payload={
            "estimated_tokens": 100,
            "full_session_tokens": 120,
            "included_turn_refs": [],
            "excluded_turn_refs": [],
        },
        model_visible=False,
    )
    recorder.append(
        "tool_result",
        tool_name="edit_image",
        execution_ref="exec_edit",
        status="succeeded",
        effect="none",
        payload={
            "result": {"status": "prepared", "kind": "image", "bytes": len(output_bytes)},
            "evidence": {
                "attachments": [
                    {
                        "attachment_ref": reference_ref,
                        "mime": "image/png",
                        "bytes": len(reference_bytes),
                        "source": "page_capture",
                        "index": 1,
                        "label": "Web reference 1 sent to image model",
                        "description": "Dark braid and period clothing.",
                    },
                    {
                        "attachment_ref": output_ref,
                        "mime": "image/png",
                        "bytes": len(output_bytes),
                        "source": "image_edit",
                        "index": 1,
                        "label": "Edited image result",
                    },
                ]
            },
        },
        model_visible=True,
    )
    recorder.record_assistant_output("hi")
    await agent_events.persist_agent_events(turn.id, recorder.events)
    await session_manager.finish_turn(turn.id, status="completed", final_text="hi")
    service = AgentAdminService(
        LLMChatConfig(),
        [{"name": "send_msg", "source_hash": "send"}, {"name": "web_search", "source_hash": "web"}],
        attachment_root=tmp_path,
    )
    app = FastAPI()
    asset_dir = Path(__file__).resolve().parents[1] / "plugins" / "llm_chat" / "webui_sessions"
    app.include_router(create_agent_sessions_router(service, asset_dir=asset_dir))
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            page = await client.get("/api/llm-chat/sessions/page")
            assert page.status_code == 200
            assert "img-src 'self' blob:" in page.headers["content-security-policy"]

            scopes = (await client.get("/api/llm-chat/sessions/scopes")).json()["items"]
            assert scopes[0]["scope_ref"] == scope.scope_ref

            sessions = (await client.get(f"/api/llm-chat/sessions/scopes/{scope.scope_ref}/sessions")).json()["items"]
            assert sessions[0]["session_ref"] == context_session.session_ref

            turns = (await client.get(f"/api/llm-chat/sessions/sessions/{context_session.session_ref}/turns")).json()[
                "items"
            ]
            assert turns[0]["turn_ref"] == turn.turn_ref
            assert turns[0]["input_preview"] == "hello"
            assert turns[0]["model"] is None
            assert turns[0]["model_call_count"] is None
            assert turns[0]["tool_call_count"] == 1

            events = (await client.get(f"/api/llm-chat/sessions/turns/{turn.turn_ref}/events")).json()["items"]
            assert [event["event_type"] for event in events] == [
                "user_input",
                "context_selection",
                "tool_result",
                "assistant_output",
            ]
            assert events[0]["images"][0]["url"].endswith(
                f"/events/{events[0]['event_ref']}/attachments/{attachment_ref}"
            )
            attachment = await client.get(events[0]["images"][0]["url"])
            assert attachment.status_code == 200
            assert attachment.content == attachment_bytes
            assert attachment.headers["content-type"] == "image/png"
            assert attachment.headers["cache-control"] == "private, no-store"
            missing_attachment = await client.get(
                f"/api/llm-chat/sessions/events/{events[0]['event_ref']}/attachments/input_cccccccccccccccccccccccccccccccc"
            )
            assert missing_attachment.status_code == 404
            edit_images = events[2]["images"]
            assert [image["name"] for image in edit_images] == [
                "Web reference 1 sent to image model",
                "Edited image result",
            ]
            assert edit_images[0]["text"] == "Dark braid and period clothing."
            reference = await client.get(edit_images[0]["url"])
            output = await client.get(edit_images[1]["url"])
            assert reference.status_code == output.status_code == 200
            assert reference.content == reference_bytes
            assert output.content == output_bytes

            inspector = (await client.get(f"/api/llm-chat/sessions/turns/{turn.turn_ref}/context")).json()["item"]
            assert inspector["selection"]["estimated_tokens"] == 100
            assert inspector["baseline"]["tool_schema_hash"] == "tools"
            assert inspector["captured"] is False
            assert inspector["budgets"] == {}
            assert inspector["current_limits"]["max_input_tokens"] == service.config.max_input_tokens
            inspection = (await client.get(f"/api/llm-chat/sessions/turns/{turn.turn_ref}/inspection")).json()["item"]
            assert inspection["model_calls"] == []
            assert inspection["persona"] is None
            assert inspection["usage"]["total_tokens"] is None
            assert inspection["usage"]["source"] == "not_recorded"
            assert inspection["tool_calls"][0]["result_event_ref"] == events[2]["event_ref"]
            assert [output["event_ref"] for output in inspection["outputs"]] == [
                events[3]["event_ref"],
            ]
            assert all(output["source"] == "legacy_incomplete" for output in inspection["outputs"])
            detail = (await client.get(f"/api/llm-chat/sessions/sessions/{context_session.session_ref}")).json()["item"]
            assert detail["context"]["estimated_tokens"] == 100
            assert detail["context"]["max_input_tokens"] is None
            assert detail["usage"]["input_tokens"] is None
            assert detail["persona"] is None
            prior_events = len(recorder.events)
            recorder.append(
                "model_request",
                attempt=1,
                payload={
                    "request_id": "observed",
                    "model": "captured-model",
                    "messages": [{"content": "context" * 10000}],
                    "capture_status": "complete",
                },
                model_visible=False,
            )
            recorder.append(
                "model_response",
                attempt=1,
                status="succeeded",
                payload={
                    "request_id": "observed",
                    "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                },
                model_visible=False,
            )
            recorder.append("model_attempt", attempt=1, payload={"metrics": {"total_tokens": 999}}, model_visible=False)
            recorder.append(
                "context_snapshot",
                payload={
                    "system": "scaffold" * 10000,
                    "selection": {"estimated_tokens": 123, "included_turn_refs": ["past"], "excluded_turn_refs": []},
                    "budgets": {"max_input_tokens": 456, "output_reserve_tokens": 78},
                },
                model_visible=False,
            )
            await agent_events.persist_agent_events(turn.id, recorder.events[prior_events:])
            updated_turns = (
                await client.get(f"/api/llm-chat/sessions/sessions/{context_session.session_ref}/turns")
            ).json()["items"]
            assert updated_turns[0]["model"] == "captured-model"
            assert updated_turns[0]["model_call_count"] == 1
            updated_detail = (
                await client.get(f"/api/llm-chat/sessions/sessions/{context_session.session_ref}")
            ).json()["item"]
            assert updated_detail["usage"]["total_tokens"] == 15
            assert updated_detail["usage"]["measured_requests"] == 1
            assert updated_detail["context"]["max_input_tokens"] == 456
            assert updated_detail["context"]["estimated_tokens"] == 123
            assert updated_detail["context"]["included_count"] == 1
            assert updated_detail["context"]["captured"] is True

            payload = (
                await client.get(
                    f"/api/llm-chat/sessions/events/{events[0]['event_ref']}/payload",
                    params={"path": "content"},
                )
            ).json()["item"]
            assert payload["data"] == "hello"

            rollover = await client.post(
                f"/api/llm-chat/sessions/scopes/{scope.scope_ref}/sessions/{context_session.session_ref}/rollover",
                json={"carry_handoff": False},
            )
            assert rollover.status_code == 200
            continued_ref = rollover.json()["item"]["session_ref"]
            assert continued_ref != context_session.session_ref
            continued = (await client.get(f"/api/llm-chat/sessions/sessions/{continued_ref}")).json()["item"]
            assert continued["persona"]["key"] == service.config.default_persona

            rejected_reset = await client.post(
                f"/api/llm-chat/sessions/scopes/{scope.scope_ref}/hard-reset",
                json={"confirmation": "no"},
            )
            assert rejected_reset.status_code == 400

            reset = await client.post(
                f"/api/llm-chat/sessions/scopes/{scope.scope_ref}/hard-reset",
                json={"confirmation": "CONFIRM"},
            )
            assert reset.status_code == 200
            assert reset.json()["item"]["status"] == "active"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_event_payload_pages_reconstruct_objects_and_arrays(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"nested": [{"text": "珂朵莉" * 100, "index": index} for index in range(4)]}
    event = AgentEvent(event_ref="event_page", payload_json=json.dumps(payload, ensure_ascii=False))

    async def lookup(_ref: str) -> AgentEvent:
        return event

    monkeypatch.setattr(AgentAdminService, "_event", staticmethod(lookup))
    service = AgentAdminService(LLMChatConfig(), [])
    for path, expected in (("", payload), ("nested", payload["nested"])):
        offset = 0
        chunks = []
        while True:
            page = await service.read_event_payload(event.event_ref, path=path, offset=offset, limit=256)
            assert page["format"] == "text"
            assert page["offset"] == offset
            chunks.append(page["data"])
            if page["next_offset"] is None:
                break
            assert page["next_offset"] > offset
            offset = page["next_offset"]
        serialized = "".join(chunks)
        assert json.loads(serialized) == expected
        assert len(serialized) == page["total_chars"]
        exhausted = await service.read_event_payload(event.event_ref, path=path, offset=len(serialized), limit=256)
        assert exhausted["data"] == ""
        assert exhausted["next_offset"] is None
    whole = await service.read_event_payload(event.event_ref, limit=100000)
    assert whole["format"] == "json"
    assert whole["data"] == payload
    assert whole["next_offset"] is None
    exact_boundary = await service.read_event_payload(event.event_ref, limit=whole["total_chars"])
    assert exact_boundary["format"] == "json"
    assert exact_boundary["data"] == payload


def test_usage_ignores_outer_aggregate_when_requests_are_captured() -> None:
    def event(kind: str, payload: dict, *, attempt: int = 1, turn_id: int = 1) -> AgentEvent:
        return AgentEvent(
            event_type=kind,
            event_ref=f"{kind}_{attempt}_{turn_id}",
            turn_id=turn_id,
            attempt=attempt,
            payload_json=json.dumps(payload),
        )

    events = [
        event("model_request", {"request_id": "first"}),
        event(
            "model_response",
            {
                "request_id": "first",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "total_tokens": 15,
                    "cached_input_tokens": 4,
                    "reasoning_tokens": 2,
                },
            },
        ),
        event("model_request", {"request_id": "second"}),
        event("model_response", {"request_id": "second", "usage": None}),
        event("model_attempt", {"metrics": {"input_tokens": 999, "output_tokens": 999, "total_tokens": 1998}}),
        event("model_attempt", {"metrics": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5}}, attempt=2),
    ]
    usage = session_inspection.summarize_usage(events)
    assert (usage["input_tokens"], usage["output_tokens"], usage["total_tokens"]) == (12, 8, 20)
    assert (usage["cached_input_tokens"], usage["reasoning_tokens"]) == (4, 2)
    assert (usage["measured_requests"], usage["unknown_requests"]) == (1, 1)
    assert usage["source"] == "mixed"
    assert usage["coverage"]["legacy_request_count_unknown"] is True
    assert usage["coverage"]["complete"] is False
    # Identical outer attempt numbers in other turns must not hide legacy usage.
    other_turn = event("model_attempt", {"metrics": {"total_tokens": 7}}, turn_id=2)
    assert session_inspection.summarize_usage(events + [other_turn])["total_tokens"] == 27


def test_terminal_turn_does_not_label_missing_call_results_as_running() -> None:
    request = AgentEvent(
        event_ref="request",
        event_type="model_request",
        turn_id=1,
        attempt=1,
        payload_json=json.dumps({"request_id": "unfinished", "model": "recorded"}),
    )
    tool = AgentEvent(
        event_ref="tool",
        event_type="assistant_tool_call",
        execution_ref="execution",
        attempt=1,
        tool_name="search",
        payload_json="{}",
        effect="none",
    )
    assert session_inspection.project_model_calls([request], turn_status="cancelled")[0]["status"] == "not_recorded"
    assert session_inspection.project_tool_calls([tool], turn_status="completed")[0]["status"] == "not_recorded"
    assert session_inspection.project_model_calls([request], turn_status="running")[0]["status"] == "running"
    assert session_inspection.project_tool_calls([tool], turn_status="running")[0]["status"] == "running"


@pytest.mark.asyncio
async def test_confirmed_delivery_inspection_preserves_prefix_and_receipt_authorization(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    for module in (agent_admin, agent_events, session_manager, session_inspection, personality):
        monkeypatch.setattr(module, "get_session", session_factory)
    scope = await session_manager.get_or_create_scope(ScopeIdentity("test", "bot", "guild", "delivery", "Delivery"))
    context_session = await session_manager.create_session(
        scope.id,
        BaselineFingerprint("test", "persona", "system", "tools", "policy"),
        start_reason="initial",
    )
    turn = await session_manager.start_turn(
        context_session,
        trigger_message_id="input",
        user_id="alice",
        user_name="Alice",
        conversation_user_id=None,
        fresh_context=False,
    )
    received = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)
    output_ref = "output_" + "a" * 32
    reference_ref = "reference_" + "b" * 32
    image_bytes = b"private-confirmed-render"
    (tmp_path / f"{output_ref}.png").write_bytes(image_bytes)
    (tmp_path / f"{reference_ref}.png").write_bytes(b"private-input-reference")
    recorder = AgentTurnRecorder()
    recorder.append(
        "turn_timing",
        payload={
            "received_at": received.isoformat(),
            "timing_source": "received_to_confirmed_delivery",
        },
        model_visible=False,
        created_at=received,
    )
    tool = recorder.append(
        "tool_result",
        tool_name="send_msg",
        execution_ref="send-message",
        status="succeeded",
        effect="unknown",
        payload={"evidence": {"attachments": [{"attachment_ref": reference_ref, "mime": "image/png"}]}},
    )
    image_delivery = recorder.append(
        "message_delivery",
        role="assistant",
        execution_ref="send-message",
        status="confirmed",
        effect="confirmed",
        duration_ms=3000,
        model_visible=False,
        created_at=received + timedelta(seconds=3),
        payload={
            "content": "",
            "attachments": [{"attachment_ref": output_ref, "mime": "image/png"}],
            "capture_status": "captured",
            "received_at": received.isoformat(),
            "confirmed_at": (received + timedelta(seconds=3)).isoformat(),
        },
    )
    text_delivery = recorder.append(
        "message_delivery",
        role="assistant",
        status="confirmed",
        effect="confirmed",
        duration_ms=7000,
        model_visible=False,
        created_at=received + timedelta(seconds=7),
        payload={
            "content": "<script>alert('literal text')</script>\nDelivered prefix.",
            "attachments": [],
            "capture_status": "captured",
            "received_at": received.isoformat(),
            "confirmed_at": (received + timedelta(seconds=7)).isoformat(),
            "media": [
                {
                    "kind": "audio",
                    "label": "Voice message",
                    "capture_status": "not_recorded",
                    "url": "https://must-not-project.invalid",
                    "path": "/must-not-project",
                }
            ],
        },
    )
    recorder.append(
        "message_delivery",
        role="assistant",
        status="failed",
        effect="unknown",
        duration_ms=9000,
        payload={"content": "Undelivered suffix"},
        model_visible=False,
    )
    recorder.append(
        "model_response",
        payload={"request_id": "late", "content": "Model finalizer is not delivery"},
        created_at=received + timedelta(seconds=40),
        duration_ms=40000,
        model_visible=False,
    )
    recorder.record_assistant_output("Summary must not replace confirmed prefix or invent suffix.")
    await agent_events.persist_agent_events(turn.id, recorder.events)
    await session_manager.finish_turn(turn.id, status="partial", final_text="Legacy summary")
    async with session_factory() as db:
        stored_turn = await db.get(AgentTurn, turn.id)
        assert stored_turn is not None
        stored_turn.created_at = (received + timedelta(seconds=2)).replace(tzinfo=None)
        stored_turn.finished_at = (received + timedelta(seconds=50)).replace(tzinfo=None)
        await db.commit()
    service = AgentAdminService(LLMChatConfig(), [], attachment_root=tmp_path)
    app = FastAPI()
    app.include_router(
        create_agent_sessions_router(
            service,
            asset_dir=Path(__file__).resolve().parents[1] / "plugins" / "llm_chat" / "webui_sessions",
        )
    )
    persisted = {event.sequence: event for event in await agent_events.load_turn_events(turn.id)}
    image_event = persisted[image_delivery.sequence]
    text_event = persisted[text_delivery.sequence]
    tool_event = persisted[tool.sequence]
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            inspection = (await client.get(f"/api/llm-chat/sessions/turns/{turn.turn_ref}/inspection")).json()["item"]
            turns = (await client.get(f"/api/llm-chat/sessions/sessions/{context_session.session_ref}/turns")).json()[
                "items"
            ]
            for summary in (inspection["turn"], turns[0]):
                assert summary["duration_ms"] == 7000
                assert summary["duration_source"] == "received_to_confirmed_delivery"
                assert summary["received_at"] == received.isoformat()
                assert summary["last_delivery_at"] == (received + timedelta(seconds=7)).isoformat()
            outputs = inspection["outputs"]
            assert [output["event_ref"] for output in outputs] == [image_event.event_ref, text_event.event_ref]
            assert outputs[1]["content"] == "<script>alert('literal text')</script>\nDelivered prefix."
            assert outputs[1]["media"] == [
                {"kind": "audio", "label": "Voice message", "capture_status": "not_recorded"}
            ]
            linked = inspection["tool_calls"][0]["deliveries"]
            assert [delivery["event_ref"] for delivery in linked] == [image_event.event_ref]
            image_url = linked[0]["images"][0]["url"]
            assert image_url == f"/api/llm-chat/sessions/events/{image_event.event_ref}/attachments/{output_ref}"
            assert outputs[0]["images"][0]["url"] == image_url
            image = await client.get(image_url)
            assert image.content == image_bytes
            assert image.headers["cache-control"] == "private, no-store"
            for event_ref, attachment_ref in (
                (tool_event.event_ref, output_ref),
                (image_event.event_ref, reference_ref),
            ):
                denied = await client.get(f"/api/llm-chat/sessions/events/{event_ref}/attachments/{attachment_ref}")
                assert denied.status_code == 404
    finally:
        await engine.dispose()


def test_turn_timing_distinguishes_unknown_legacy_running_and_no_delivery() -> None:
    started = datetime.now(timezone.utc) - timedelta(seconds=10)
    turn = AgentTurn(status="completed", created_at=None, finished_at=None)
    timing = session_inspection.project_turn_timing(turn, [])
    assert timing["duration_ms"] is None
    assert timing["duration_source"] == "not_recorded"
    turn.created_at = started.replace(tzinfo=None)
    assert session_inspection.project_turn_timing(turn, [])["duration_ms"] is None
    turn.finished_at = (started + timedelta(seconds=5)).replace(tzinfo=None)
    legacy = session_inspection.project_turn_timing(turn, [])
    assert (legacy["duration_ms"], legacy["duration_source"]) == (5000, "legacy_lifecycle")
    assert legacy["received_at"] is legacy["last_delivery_at"] is None
    event = AgentEvent(
        event_type="turn_timing", sequence=1, payload_json=json.dumps({"received_at": started.isoformat()})
    )
    no_delivery = session_inspection.project_turn_timing(turn, [event])
    assert no_delivery["duration_ms"] is None
    assert no_delivery["duration_source"] == "not_delivered"
    assert (
        session_inspection.project_outputs(
            [
                event,
                AgentEvent(
                    event_type="assistant_output",
                    sequence=2,
                    effect="confirmed",
                    payload_json='{"content":"not a receipt"}',
                ),
            ]
        )
        == []
    )
    turn.status = "running"
    running = session_inspection.project_turn_timing(turn, [event])
    assert running["duration_ms"] is None
    assert running["duration_source"] == "running"
    assert running["elapsed_ms"] >= 10000
    receipt = AgentEvent(
        event_type="message_delivery",
        sequence=2,
        status="confirmed",
        effect="confirmed",
        duration_ms=None,
        created_at=None,
        payload_json='{"received_at":"invalid","confirmed_at":"invalid"}',
    )
    turn.status = "completed"
    unknown = session_inspection.project_turn_timing(turn, [receipt])
    assert unknown["duration_ms"] is None
    assert unknown["duration_source"] == "not_recorded"
    assert unknown["received_at"] is unknown["last_delivery_at"] is None


@pytest.mark.asyncio
async def test_relationship_inspection_preserves_snapshots_batch_states_and_authenticated_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    for module in (agent_admin, agent_events, session_manager, session_inspection, personality):
        monkeypatch.setattr(module, "get_session", session_factory)
    scope = await session_manager.get_or_create_scope(ScopeIdentity("test", "bot", "guild", "affect", "Affect"))
    baseline = BaselineFingerprint("test", "persona", "system", "tools", "policy")
    previous_session = await session_manager.create_session(scope.id, baseline, start_reason="initial")
    first = await session_manager.start_turn(
        previous_session,
        trigger_message_id="first",
        user_id="alice",
        user_name="Alice",
        conversation_user_id=None,
        fresh_context=False,
    )
    await session_manager.finish_turn(first.id, status="completed", final_text="Confirmed first reply")
    current_session = await session_manager.create_session(scope.id, baseline, start_reason="webui_new")
    current = await session_manager.start_turn(
        current_session,
        trigger_message_id="second",
        user_id="alice",
        user_name="Alice",
        conversation_user_id=None,
        fresh_context=False,
    )
    await session_manager.finish_turn(current.id, status="silent", final_text="")
    unrelated = await session_manager.start_turn(
        current_session,
        trigger_message_id="other",
        user_id="bob",
        user_name="Bob",
        conversation_user_id=None,
        fresh_context=False,
    )
    now = datetime.now(timezone.utc).isoformat()
    captured = {
        "version": 3,
        "axes": {"affection": 41, "trust": 50, "dependence": 22, "resentment": 15, "familiarity": 60},
        "description": "Private relationship description",
        "impression": "Earlier impression",
        "emotions": [
            {
                "name": "hurt",
                "intensity": 0.7,
                "cause": "<script>literal emotional cause</script>",
                "evidence_turn_ids": [first.id],
                "updated_at": 100.0,
            }
        ],
        "updated_at": now,
        "processed_turn_id": 0,
    }
    before = {**captured, "version": 4, "axes": {**captured["axes"], "resentment": 20}}
    after = {
        **before,
        "version": 5,
        "axes": {**before["axes"], "resentment": 8},
        "emotions": [],
        "description": "Repaired after considering both turns",
        "processed_turn_id": current.id,
    }
    evaluation = {
        "evaluation_ref": "shared-batch",
        "evidence_turn_ids": [first.id, current.id],
        "before": None,
        "after": None,
        "model": "evaluator-alias",
        "error": None,
        "queued_at": now,
        "started_at": None,
        "finished_at": None,
        "changes": {},
    }
    for turn in (first, current):
        recorder = AgentTurnRecorder()
        recorder.append(
            "persona_state",
            payload={"relationship": captured, "memory": {"private": "x" * 100000}},
            model_visible=False,
        )
        recorder.append("relationship_evaluation", payload=evaluation, status="pending", model_visible=False)
        if turn.id == current.id:
            recorder.append(
                "response_decision",
                payload={
                    "outcome": "silent",
                    "source": "model",
                    "reason": "Prefer no reply to this turn",
                    "actual_delivery": {"text_messages": 0, "media_messages": 0, "confirmed_deliveries": 0},
                },
                model_visible=False,
            )
        await agent_events.persist_agent_events(turn.id, recorder.events)
    service = AgentAdminService(LLMChatConfig(), [])
    evaluation_event_ids = [
        event.id
        for turn in (first, current)
        for event in await agent_events.load_turn_events(turn.id)
        if event.event_type == "relationship_evaluation"
    ]
    async with session_factory() as db:
        for turn, event_id in zip((first, current), evaluation_event_ids, strict=True):
            db.add(
                RelationshipEvidence(
                    turn_id=turn.id,
                    user_id=turn.user_id,
                    channel_id=scope.channel_id,
                    persona_prompt="Test persona",
                    event_id=event_id,
                    evaluation_ref="shared-batch",
                    payload_json=json.dumps(
                        {
                            "turn_id": turn.id,
                            "user": f"Private input for {turn.trigger_message_id}",
                            "assistant": "Confirmed first reply" if turn.id == first.id else "",
                            "outcome": "completed" if turn.id == first.id else "silent",
                            "delivered_media": 0,
                        }
                    ),
                )
            )
        await db.commit()

    def authenticate(request: Request) -> None:
        if request.headers.get("authorization") != "Bearer test-admin":
            raise HTTPException(status_code=401)

    app = FastAPI()
    app.include_router(
        create_agent_sessions_router(
            service,
            asset_dir=Path(__file__).resolve().parents[1] / "plugins" / "llm_chat" / "webui_sessions",
            auth_dependency=authenticate,
        )
    )
    path = f"/api/llm-chat/sessions/turns/{current.turn_ref}/inspection"
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get(path)).status_code == 401
            client.headers["authorization"] = "Bearer test-admin"
            pending = (await client.get(path)).json()["item"]
            assert pending["relationship"]["axes"]["resentment"] == 15
            assert pending["relationship"]["emotions"][0]["cause"] == captured["emotions"][0]["cause"]
            assert pending["relationship_evaluation"]["status"] == "pending"
            assert pending["relationship_evaluation"]["before"] is None
            assert pending["relationship_evaluation"]["after"] is None
            assert pending["response_decision"]["actual_outcome"] == "silent"
            links = {item["turn_id"]: item for item in pending["evidence_turns"]}
            assert links[first.id]["session_ref"] == previous_session.session_ref
            assert links[first.id]["turn_ref"] == first.turn_ref
            assert links[current.id]["turn_ref"] == current.turn_ref
            assert unrelated.id not in links
            linked_path = f"/api/llm-chat/sessions/turns/{links[first.id]['turn_ref']}/inspection"
            assert (await client.get(linked_path)).status_code == 200
            client.headers.pop("authorization")
            assert (await client.get(linked_path)).status_code == 401
            client.headers["authorization"] = "Bearer test-admin"
            event_ref = pending["relationship_evaluation"]["event_ref"]
            for status in ("running", "failed", "succeeded"):
                payload = {
                    **evaluation,
                    "before": before,
                    "started_at": now,
                    "after": after,
                    "changes": {"resentment": {"before": 20, "after": 8, "delta": -12}},
                    "error": "Provider timeout; evidence retained" if status == "failed" else None,
                    "finished_at": now if status != "running" else None,
                }
                async with session_factory() as db:
                    for event_id in evaluation_event_ids:
                        stored = await db.get(AgentEvent, event_id)
                        assert stored is not None
                        stored.status = status
                        stored.payload_json = json.dumps(payload)
                    await db.commit()
                inspection = (await client.get(path)).json()["item"]
                projected = inspection["relationship_evaluation"]
                assert projected["event_ref"] == event_ref
                assert projected["status"] == status
                assert projected["evidence_turn_ids"] == [first.id, current.id]
                assert inspection["relationship"]["axes"]["resentment"] == 15
                assert projected["before"]["axes"]["resentment"] == 20
                if status == "succeeded":
                    assert projected["after"]["axes"]["resentment"] == 8
                    assert projected["after"]["emotions"] == []
                    assert projected["changes"]["resentment"]["delta"] == -12
                else:
                    assert projected["after"] is None
                    assert projected["changes"] == {}
                if status == "failed":
                    assert projected["error"] == payload["error"]
                other = (await client.get(linked_path)).json()["item"]["relationship_evaluation"]
                assert other["evaluation_ref"] == projected["evaluation_ref"]
                assert other["evidence_turn_ids"] == projected["evidence_turn_ids"]
                listing = (
                    await client.get(f"/api/llm-chat/sessions/sessions/{current_session.session_ref}/turns")
                ).json()["items"]
                summary = next(item for item in listing if item["turn_ref"] == current.turn_ref)
                assert summary["relationship_evaluation"]["status"] == status
                assert summary["relationship_evaluation"]["evidence_count"] == 2
                assert summary["response_decision"]["actual_outcome"] == "silent"
                serialized_list = json.dumps(listing)
                assert "Private relationship description" not in serialized_list
                assert "literal emotional cause" not in serialized_list
                assert "Prefer no reply to this turn" not in serialized_list
                assert "x" * 1000 not in serialized_list
            events = (await client.get(f"/api/llm-chat/sessions/turns/{current.turn_ref}/events")).json()["items"]
            event = next(item for item in events if item["event_type"] == "relationship_evaluation")
            assert event["relationship_evaluation"]["after"]["version"] == 5
            persona = next(item for item in events if item["event_type"] == "persona_state")
            assert persona["relationship"]["version"] == persona["persona"]["relationship"]["version"] == 3
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_relationship_evidence_navigation_survives_identity_migration_without_cross_owner_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    for module in (agent_admin, agent_events, session_manager, session_inspection, personality, identity):
        monkeypatch.setattr(module, "get_session", session_factory)
    scope = await session_manager.get_or_create_scope(ScopeIdentity("test", "bot", "guild", "affect", "Affect"))
    foreign_scope = await session_manager.get_or_create_scope(
        ScopeIdentity("test", "other-bot", "guild", "affect", "Other account"),
    )
    baseline = BaselineFingerprint("test", "persona", "system", "tools", "policy")
    previous_session = await session_manager.create_session(scope.id, baseline, start_reason="initial")
    current_session = await session_manager.create_session(scope.id, baseline, start_reason="webui_new")
    foreign_session = await session_manager.create_session(foreign_scope.id, baseline, start_reason="initial")
    turns = []
    for context_session, message_id, user_id in (
        (previous_session, "legacy", "alice-old"),
        (current_session, "current", "alice-current"),
        (current_session, "another-member", "bob"),
        (foreign_session, "another-scope", "alice-current"),
        (current_session, "without-evidence", "alice-current"),
    ):
        turn = await session_manager.start_turn(
            context_session,
            trigger_message_id=message_id,
            user_id=user_id,
            user_name=user_id,
            conversation_user_id=None,
            fresh_context=False,
        )
        await session_manager.finish_turn(turn.id, status="silent", final_text="")
        turns.append(turn)
    legacy, current, other_member, foreign, unowned = turns
    now = datetime.now(timezone.utc).isoformat()
    captured = {
        "version": 7,
        "axes": {"affection": 40, "trust": 50, "dependence": 20, "resentment": 10, "familiarity": 60},
        "description": "Private state captured before identity migration",
        "impression": "Historical impression",
        "emotions": [
            {
                "name": "uncertain",
                "intensity": 0.5,
                "cause": "Private historical cause",
                "evidence_turn_ids": [legacy.id, foreign.id],
                "updated_at": 100.0,
            }
        ],
        "updated_at": now,
        "processed_turn_id": 0,
    }
    evaluation = {
        "evaluation_ref": "identity-batch",
        "evidence_turn_ids": [legacy.id, current.id, other_member.id, unowned.id],
        "before": None,
        "after": None,
        "model": "evaluator-alias",
        "error": None,
        "queued_at": now,
        "started_at": None,
        "finished_at": None,
        "changes": {},
    }
    # Untrusted references in either view cannot substitute for durable, scoped ownership.
    for turn in turns:
        recorder = AgentTurnRecorder()
        recorder.record_user_input(
            f"Private attributable input for {turn.trigger_message_id}",
            user_name=turn.user_name,
            fresh_context=False,
        )
        if turn.id in (legacy.id, current.id):
            recorder.append("persona_state", payload={"relationship": captured}, model_visible=False)
        recorder.append(
            "relationship_evaluation",
            status="pending",
            model_visible=False,
            payload=evaluation
            if turn.id in (legacy.id, current.id)
            else {
                **evaluation,
                "evaluation_ref": f"separate-{turn.id}",
                "evidence_turn_ids": [turn.id],
            },
        )
        await agent_events.persist_agent_events(turn.id, recorder.events)
        if turn.id == unowned.id:
            continue
        event = next(
            item
            for item in await agent_events.load_turn_events(turn.id)
            if item.event_type == "relationship_evaluation"
        )
        async with session_factory() as db:
            db.add(
                RelationshipEvidence(
                    turn_id=turn.id,
                    user_id=turn.user_id,
                    channel_id=scope.channel_id,
                    persona_prompt="Test persona",
                    event_id=event.id,
                    evaluation_ref="identity-batch" if turn.id in (legacy.id, current.id) else f"separate-{turn.id}",
                    payload_json=json.dumps(
                        {
                            "turn_id": turn.id,
                            "user": f"Private attributable input for {turn.trigger_message_id}",
                            "assistant": "",
                            "outcome": "silent",
                            "delivered_media": 0,
                        }
                    ),
                )
            )
            await db.commit()

    def authenticate(request: Request) -> None:
        if request.headers.get("authorization") != "Bearer test-admin":
            raise HTTPException(status_code=401)

    app = FastAPI()
    app.include_router(
        create_agent_sessions_router(
            AgentAdminService(LLMChatConfig(), []),
            asset_dir=Path(__file__).resolve().parents[1] / "plugins" / "llm_chat" / "webui_sessions",
            auth_dependency=authenticate,
        )
    )
    legacy_path = f"/api/llm-chat/sessions/turns/{legacy.turn_ref}/inspection"
    current_path = f"/api/llm-chat/sessions/turns/{current.turn_ref}/inspection"
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get(legacy_path)).status_code == 401
            client.headers["authorization"] = "Bearer test-admin"
            previous = (await client.get(legacy_path)).json()["item"]
            assert {item["turn_id"] for item in previous["evidence_turns"]} == {legacy.id}
            assert previous["relationship"]["description"] == captured["description"]
            assert previous["relationship"]["emotions"][0]["cause"] == captured["emotions"][0]["cause"]
            current_before = (await client.get(current_path)).json()["item"]
            assert {item["turn_id"] for item in current_before["evidence_turns"]} == {current.id}

            await identity.migrate_legacy_user_state(scope.channel_id, ["alice-old"], "alice-current")

            async with session_factory() as db:
                historical_turn = await db.get(AgentTurn, legacy.id)
                historical_evidence = await db.get(RelationshipEvidence, legacy.id)
                unrelated_evidence = await db.get(RelationshipEvidence, other_member.id)
                assert historical_turn is not None
                assert historical_turn.user_id == "alice-old"
                assert historical_evidence is not None
                assert historical_evidence.user_id == "alice-current"
                assert unrelated_evidence is not None
                assert unrelated_evidence.user_id == "bob"
            expected_links = {
                legacy.id: (legacy.turn_ref, previous_session.session_ref),
                current.id: (current.turn_ref, current_session.session_ref),
            }
            for path, original in ((legacy_path, previous), (current_path, current_before)):
                response = await client.get(path)
                assert response.status_code == 200
                inspection = response.json()["item"]
                assert inspection["turn"]["user_id"] == original["turn"]["user_id"]
                assert inspection["relationship"] == original["relationship"]
                assert inspection["relationship_evaluation"] == original["relationship_evaluation"]
                assert {
                    item["turn_id"]: (item["turn_ref"], item["session_ref"]) for item in inspection["evidence_turns"]
                } == expected_links
                for link in inspection["evidence_turns"]:
                    linked_path = f"/api/llm-chat/sessions/turns/{link['turn_ref']}/inspection"
                    linked = await client.get(linked_path)
                    assert linked.status_code == 200
                    assert linked.json()["item"]["turn"]["turn_ref"] == link["turn_ref"]
                    client.headers.pop("authorization")
                    assert (await client.get(linked_path)).status_code == 401
                    client.headers["authorization"] = "Bearer test-admin"
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    ("outcome", "delivery", "expected"),
    [
        ("delivered", {"text_messages": 0, "media_messages": 1, "confirmed_deliveries": 1}, "media_only"),
        ("delivered", {"text_messages": 1, "media_messages": 0, "confirmed_deliveries": 1}, "text"),
        ("delivered", {"text_messages": 1, "media_messages": 1, "confirmed_deliveries": 1}, "mixed"),
        ("silent", {"text_messages": 0, "media_messages": 0, "confirmed_deliveries": 0}, "silent"),
        ("declined", {"text_messages": 1, "media_messages": 0, "confirmed_deliveries": 1}, "refusal"),
        ("declined", {"text_messages": 0, "media_messages": 0, "confirmed_deliveries": 0}, "unknown"),
        ("silent", {}, "unknown"),
    ],
)
def test_response_projection_requires_actual_delivery_for_visible_outcomes(outcome, delivery, expected) -> None:
    event = AgentEvent(event_ref="decision", event_type="response_decision", status="recorded")
    projected = agent_event_view.event_response_decision(event, {"outcome": outcome, "actual_delivery": delivery})
    assert projected is not None
    assert projected["actual_outcome"] == expected


def test_missing_relationship_fields_and_legacy_engagement_are_not_new_state() -> None:
    legacy = AgentEvent(event_type="engagement_decision", payload_json=json.dumps({"level": "brief", "warmth": "cold"}))
    views = agent_event_view.project_relationship_views([legacy])
    assert views["relationship"] is views["relationship_evaluation"] is views["response_decision"] is None
    assert views["engagement"]["level"] == "brief"
    captured = agent_event_view.relationship_snapshot(
        {"axes": {"affection": 0, "trust": True, "resentment": float("nan")}}
    )
    assert captured is not None
    assert captured["axes"]["affection"] == 0
    assert captured["axes"]["trust"] is captured["axes"]["resentment"] is captured["axes"]["dependence"] is None
    assert captured["emotions"] is None
