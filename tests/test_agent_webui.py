"""Authenticated AgentEvent WebUI API contracts."""

from __future__ import annotations

import json
from types import SimpleNamespace
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from arclet.entari.config import EntariConfig

if not hasattr(EntariConfig, "instance"):
    setattr(EntariConfig, "instance", EntariConfig.load(Path(__file__).resolve().parents[1] / "entari.yml"))

from entari_plugin_database import Base
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from plugins.llm_chat import agent_admin, personality, agent_events, session_manager, session_inspection
from plugins.llm_chat.config import LLMChatConfig
from plugins.llm_chat.models import AgentEvent
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
        effect="confirmed",
        payload={
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
            }
        },
        model_visible=True,
    )
    recorder.record_assistant_output("hi")
    await agent_events.persist_agent_events(turn.id, recorder.events)
    await session_manager.finish_turn(turn.id, status="completed", final_text="hi")
    service = AgentAdminService(
        LLMChatConfig(),
        [{"name": "send_text", "source_hash": "send"}, {"name": "web_search", "source_hash": "web"}],
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
            assert inspection["outputs"][0]["event_ref"] == events[3]["event_ref"]
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
