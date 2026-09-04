"""Authenticated AgentEvent WebUI API contracts."""

from __future__ import annotations

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

from plugins.llm_chat import agent_admin, agent_events, session_manager
from plugins.llm_chat.config import LLMChatConfig
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
    for module in (agent_admin, agent_events, session_manager):
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
            assert "LLM 会话" in page.text
            assert "img-src 'self' blob:" in page.headers["content-security-policy"]

            scopes = (await client.get("/api/llm-chat/sessions/scopes")).json()["items"]
            assert scopes[0]["scope_ref"] == scope.scope_ref

            sessions = (await client.get(f"/api/llm-chat/sessions/scopes/{scope.scope_ref}/sessions")).json()["items"]
            assert sessions[0]["session_ref"] == context_session.session_ref

            turns = (await client.get(f"/api/llm-chat/sessions/sessions/{context_session.session_ref}/turns")).json()[
                "items"
            ]
            assert turns[0]["turn_ref"] == turn.turn_ref

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
