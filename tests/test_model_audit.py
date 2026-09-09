"""Private audit contracts: actual request boundaries, redaction and cancellation."""

from __future__ import annotations

import json
from typing import Any
import asyncio
from datetime import datetime, timedelta

import httpx
import pytest
import litellm
from agno.agent import Agent
from agno.models.litellm import LiteLLM

from plugins.llm_chat.core.agent_trace import AgentTurnRecorder
from plugins.llm_chat.core.model_audit import (
    normalize_usage,
    sanitize_audit_value,
    sanitize_model_request,
    sanitize_model_response,
)
from plugins.llm_chat.model_audit_runtime import (
    AuditedLiteLLMClient,
    model_audit_scope,
    capture_completion,
    install_model_http_audit,
)


def test_private_snapshot_preserves_text_schemas_and_reports_omissions() -> None:
    schema = {
        "type": "function",
        "function": {
            "name": "lookup",
            "parameters": {
                "type": "object",
                "properties": {"token": {"type": "string", "description": "Search token"}, "key": {"type": "string"}},
            },
        },
    }
    exact = "Keep  whitespace\n\nand Unicode 雪 exactly."
    snapshot = sanitize_model_request(
        [
            {"role": "system", "content": exact},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_real",
                        "function": {"name": "lookup", "arguments": '{"key":"record-a","api_key":"hidden-secret"}'},
                    }
                ],
            },
            {
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,private-pixels"}}],
            },
        ],
        tools=[schema],
        parameters={
            "api_key": "hidden-secret",
            "headers": {"Authorization": "Bearer hidden-secret"},
            "max_tokens": 1024,
            "persona": {"key": "chtholly"},
        },
    )
    encoded = json.dumps(snapshot)
    assert "hidden-secret" not in encoded
    assert "private-pixels" not in encoded
    assert snapshot["messages"][0]["content"] == exact
    assert snapshot["tools"] == [schema]
    assert snapshot["parameters"]["persona"] == {"key": "chtholly"}
    assert snapshot["parameters"]["max_tokens"] == 1024
    arguments = json.loads(snapshot["messages"][1]["tool_calls"][0]["function"]["arguments"])
    assert arguments["key"] == "record-a"
    assert snapshot["capture_status"] == "redacted"
    assert any(item["path"] == "$.messages[2].content[0]" for item in snapshot["redactions"])
    response = sanitize_model_response(
        {
            "choices": [
                {
                    "message": {"content": "Public answer", "reasoning_content": "private-thought"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 12, "completion_tokens": 4, "prompt_tokens_details": {"cached_tokens": 3}},
        }
    )
    assert response["content"] == "Public answer"
    assert "private-thought" not in json.dumps(response)
    assert response["usage"] == {
        "input_tokens": 12,
        "output_tokens": 4,
        "total_tokens": 16,
        "cached_input_tokens": 3,
        "reasoning_tokens": None,
    }
    assert normalize_usage(None)["total_tokens"] is None
    overflow = sanitize_audit_value("x" * 2_000_001)
    assert overflow["capture_status"] == "overflow"
    assert overflow["data"] == {"omitted": "size_limit", "size": 2_000_001}


@pytest.mark.asyncio
async def test_real_agno_tool_loop_records_each_http_request_without_sdk_duplicates() -> None:
    recorder = AgentTurnRecorder()
    recorder.next_attempt()
    committed = []

    async def persist(events):
        committed.extend(events)

    recorder.sink = persist
    observed = []
    tool_runs = []

    async def lookup(key: str) -> str:
        """Look up the value for a record key."""
        tool_runs.append(key)
        return "result-for-" + key

    async def provider(request: httpx.Request) -> httpx.Response:
        observed.append(json.loads(request.content))
        # The operator can already see the request while its HTTP call runs.
        assert committed[-1].event_type == "model_request"
        first = len(observed) == 1
        message: dict[str, Any] = {"role": "assistant", "content": None if first else "Found result-for-record-a"}
        if first:
            message["tool_calls"] = [
                {
                    "id": "call_actual_1",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"key":"record-a"}'},
                }
            ]
        return httpx.Response(
            200,
            json={
                "id": f"chatcmpl-{len(observed)}",
                "object": "chat.completion",
                "created": 1,
                "model": "test-model",
                "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if first else "stop"}],
                "usage": {
                    "prompt_tokens": 10 * len(observed),
                    "completion_tokens": 2,
                    "total_tokens": 10 * len(observed) + 2,
                },
            },
        )

    restore = install_model_http_audit()
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:

            class Client:
                async def acompletion(self, **kwargs):
                    response = await http.post("https://provider.invalid/v1/chat/completions", json=kwargs)
                    return litellm.ModelResponse(**response.json())

            model = LiteLLM(id="test-model", client=AuditedLiteLLMClient(Client()))
            with model_audit_scope(recorder):
                result = await Agent(model=model, tools=[lookup]).arun("Look up record-a")
    finally:
        restore()
    assert result.content == "Found result-for-record-a"
    assert tool_runs == ["record-a"]
    requests = [event for event in recorder.events if event.event_type == "model_request"]
    responses = [event for event in recorder.events if event.event_type == "model_response"]
    assert len(requests) == len(responses) == 2
    assert len({event.payload["request_id"] for event in requests}) == 2
    assert [event.payload["request_id"] for event in requests] == [event.payload["request_id"] for event in responses]
    assert requests[0].payload["tools"][0]["function"]["name"] == "lookup"
    assert any(
        message["role"] == "tool" and message["tool_call_id"] == "call_actual_1"
        for message in requests[1].payload["messages"]
    )
    assert [event.payload["usage"]["input_tokens"] for event in responses] == [10, 20]
    assert all(event.payload["capture_boundary"] == "http" for event in requests)
    assert all(not event.model_visible for event in recorder.events)
    assert [event.sequence for event in committed] == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_http_retry_failure_and_cancelled_request_each_remain_paired() -> None:
    recorder = AgentTurnRecorder()
    recorder.next_attempt()
    started = asyncio.Event()
    count = 0

    async def provider(request):
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(503, json={"error": {"message": "temporary error; api_key=hidden-secret"}})
        started.set()
        await asyncio.Event().wait()

    restore = install_model_http_audit()
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:

            async def retrying_client(**kwargs):
                await client.post("https://provider.invalid/v1/chat/completions", json=kwargs)
                return await client.post("https://provider.invalid/v1/chat/completions", json=kwargs)

            with model_audit_scope(recorder):
                task = asyncio.create_task(
                    capture_completion(retrying_client, model="test", messages=[{"role": "user", "content": "hello"}])
                )
                await started.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
    finally:
        restore()
    requests = [event for event in recorder.events if event.event_type == "model_request"]
    responses = [event for event in recorder.events if event.event_type == "model_response"]
    assert [event.status for event in responses] == ["failed", "cancelled"]
    assert [event.payload["request_id"] for event in requests] == [event.payload["request_id"] for event in responses]
    assert requests[0].payload["completion_id"] == requests[1].payload["completion_id"]
    assert "hidden-secret" not in json.dumps([event.payload for event in recorder.events])


@pytest.mark.asyncio
async def test_incremental_flush_freezes_sequence_while_tools_finish() -> None:
    recorder = AgentTurnRecorder()
    entered = asyncio.Event()
    release = asyncio.Event()
    committed = []

    async def persist(events):
        entered.set()
        await release.wait()
        committed.extend(events)

    recorder.sink = persist
    now = datetime.utcnow()
    recorder.append("model_request", created_at=now)
    flushing = asyncio.create_task(recorder.flush())
    await entered.wait()
    recorder.append("tool_result", created_at=now - timedelta(seconds=1))
    recorder._resequence_chronologically()
    release.set()
    await flushing
    await recorder.flush()
    assert [(event.sequence, event.event_type) for event in committed] == [(1, "model_request"), (2, "tool_result")]
    assert recorder.pending_events() == ()
