"""Focused native reminder schema, identity, and effect-policy checks."""

from __future__ import annotations

from types import SimpleNamespace
import asyncio
from pathlib import Path
from datetime import datetime, timezone, timedelta

import pytest
from arclet.entari.config import EntariConfig

if not hasattr(EntariConfig, "instance"):
    setattr(
        EntariConfig, "instance", EntariConfig.load(Path(__file__).resolve().parents[1] / "entari.full.example.yml")
    )

from plugins.llm_chat.reminders import tool
from plugins.llm_chat.agent_context import AgentAccessContext, agent_access_scope
from plugins.llm_chat.core.tool_trace import llm_chat_tool_trace_scope, llm_chat_tool_execution_scope
from plugins.llm_chat.reminders.runtime import (
    ReminderDecision,
    reminder_decision_scope,
    current_reminder_decision,
    propose_reminder_decision,
)
from plugins.llm_chat.tools.finish_turn import _check_effects
from plugins.llm_chat.core.tool_trace_policy import (
    DeliverySnapshot,
    tool_error_effect,
    project_tool_success,
)

UTC = timezone.utc


def _session(*, direct: bool = False) -> SimpleNamespace:
    channel_type = "direct" if direct else "text"
    return SimpleNamespace(
        account=SimpleNamespace(platform="test", self_id="bot-1"),
        channel=SimpleNamespace(id="channel-1", type=channel_type),
        event=SimpleNamespace(
            channel=SimpleNamespace(id="channel-1", type=channel_type),
            user=SimpleNamespace(id="platform-user", name="User"),
            message=SimpleNamespace(id="message-1"),
        ),
    )


def _access(**kwargs: object) -> AgentAccessContext:
    values: dict[str, object] = {
        "scope_id": 1,
        "session_id": 2,
        "turn_id": 3,
        "user_id": "unified-user",
        "capability_names": frozenset({"reminder"}),
        "raw_user_text": "remind me",
    }
    values.update(kwargs)
    return AgentAccessContext(**values)


def _schedule() -> str:
    return (datetime.now(UTC) + timedelta(hours=1)).isoformat()


def test_request_schema_is_discriminated_and_forbids_identity_fields() -> None:
    schema = tool.reminder_request_json_schema()
    assert schema.get("discriminator", {}).get("propertyName") == "action"
    assert len(schema.get("oneOf", ())) == 6
    assert all(item.get("additionalProperties") is False for item in schema["$defs"].values())
    with pytest.raises(tool.DeliveryRejected):
        tool.validate_reminder_request({"action": "list", "account_id": "bot-1"})
    with pytest.raises(tool.DeliveryRejected):
        tool.validate_reminder_request({"action": "unknown"})


@pytest.mark.asyncio
async def test_create_binds_host_identity_and_requires_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    class Task:
        task_ref = "reminder_opaque"
        status = "active"
        revision = 0
        due_at = datetime.now(UTC).replace(tzinfo=None)
        timezone = "UTC"
        mode = "once"

    seen: dict[str, object] = {}

    async def create_task(**kwargs: object) -> Task:
        seen.update(kwargs)
        return Task()

    monkeypatch.setattr(tool.store, "create_task", create_task)
    request = {"action": "create", "goal": "check in", "schedule": _schedule(), "timezone": "UTC"}
    with agent_access_scope(_access()), llm_chat_tool_execution_scope("execution-1"):
        result = await tool.reminder(_session(), request)
    assert '"effect":"confirmed"' in result
    assert seen["account_id"] == "bot-1"
    assert seen["unified_user_id"] == "unified-user"
    assert seen["source_channel_id"] == "channel-1"
    assert seen["operation_id"] == "execution-1"

    with agent_access_scope(_access(capability_names=frozenset())):
        with pytest.raises(tool.DeliveryRejected, match="not enabled"):
            await tool.reminder(_session(), {"action": "list"})


def test_reminder_effect_policy_distinguishes_list_write_and_resolve() -> None:
    before = DeliverySnapshot()
    after = DeliverySnapshot()
    status, effect, _ = project_tool_success(
        "reminder",
        '{"task_ref":"reminder_opaque","status":"active","revision":1,"effect":"confirmed"}',
        before=before,
        after=after,
    )
    assert status == "succeeded"
    assert effect == "confirmed"
    assert (
        tool_error_effect("reminder", before, after, terminal_status="failed", arguments={"action": "list"})
        == "observed"
    )
    assert (
        tool_error_effect("reminder", before, after, terminal_status="failed", arguments={"action": "update"})
        == "unknown"
    )


def test_finish_silent_rejects_confirmed_write_but_accepts_failed_list() -> None:
    class Trace:
        events = []

    failed_list = SimpleNamespace(
        tool_name="reminder", status="failed", effect="observed", arguments={"action": "list"}
    )
    Trace.events = [failed_list]
    recorder = Trace()
    with llm_chat_tool_trace_scope(recorder):
        _check_effects("silent")

    confirmed_write = SimpleNamespace(
        tool_name="reminder", status="succeeded", effect="confirmed", arguments={"action": "update"}
    )
    Trace.events = [confirmed_write]
    with llm_chat_tool_trace_scope(recorder), pytest.raises(tool.DeliveryRejected, match="committed"):
        _check_effects("silent")


@pytest.mark.asyncio
async def test_resolve_proposals_are_shared_with_child_tasks_but_generation_local() -> None:
    first = ReminderDecision("first", "completed")
    second = ReminderDecision("second", "completed")
    released = asyncio.Event()

    async def propose(decision: ReminderDecision) -> ReminderDecision:
        await asyncio.sleep(0)
        return propose_reminder_decision(decision)

    async def late() -> None:
        await released.wait()
        propose_reminder_decision(second)

    assert current_reminder_decision() is None
    with pytest.raises(ValueError, match="active scheduled generation"):
        propose_reminder_decision(first)
    escaped = None
    try:
        with reminder_decision_scope():
            escaped = asyncio.create_task(late())
            await asyncio.create_task(propose(first))
            assert current_reminder_decision() == first
            with pytest.raises(ValueError, match="already been proposed"):
                await asyncio.create_task(propose(second))
            with reminder_decision_scope():
                assert current_reminder_decision() is None
                await asyncio.create_task(propose(second))
                assert current_reminder_decision() == second
            assert current_reminder_decision() == first
        released.set()
        with pytest.raises(ValueError, match="active scheduled generation"):
            await escaped
    finally:
        if escaped is not None:
            escaped.cancel()
            await asyncio.gather(escaped, return_exceptions=True)
    assert current_reminder_decision() is None


@pytest.mark.asyncio
async def test_simultaneous_generations_keep_distinct_decisions() -> None:
    arrived = [asyncio.Event(), asyncio.Event()]
    decisions = [ReminderDecision("first", "completed"), ReminderDecision("second", "paused")]

    async def child(index: int) -> None:
        propose_reminder_decision(decisions[index])
        arrived[index].set()
        await asyncio.wait_for(arrived[1 - index].wait(), timeout=1)

    async def generation(index: int) -> ReminderDecision | None:
        with reminder_decision_scope():
            await asyncio.create_task(child(index))
            return current_reminder_decision()

    assert await asyncio.gather(generation(0), generation(1)) == decisions
    assert current_reminder_decision() is None


@pytest.mark.asyncio
async def test_scheduled_continuation_uses_persisted_wakeup_history(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}
    task = SimpleNamespace(
        task_ref="reminder_current",
        mode="adaptive",
        timezone="UTC",
        revision=1,
        delivery_target_type="channel",
        delivery_channel_id="channel-1",
    )

    async def owned(_owner, _ref):
        return task

    async def history(_ref):
        return (datetime.now(UTC) - timedelta(minutes=15),)

    def schedule(value, zone, *, now, access, previous_wakeups):
        captured["previous_wakeups"] = tuple(previous_wakeups)
        return SimpleNamespace(utc=now + timedelta(minutes=30))

    monkeypatch.setattr(tool, "_owned_task", owned)
    monkeypatch.setattr(tool.store, "get_wakeup_history", history)
    monkeypatch.setattr(tool, "_require_model_schedule", schedule)
    monkeypatch.setattr(tool, "propose_reminder_decision", lambda decision: captured.update(decision=decision))
    access = _access(trigger_kind="scheduled", reminder_task_ref=task.task_ref)
    owner = tool.BoundReminderOwner(
        access, "test", "bot-1", 1, "channel-1", "unified-user", "platform-user", "", "", False
    )
    request = tool.ResolveReminder(action="resolve", decision="continue", schedule=_schedule(), timezone="UTC")
    result = await tool._resolve(owner, request)
    assert result["proposed"] is True
    assert len(captured["previous_wakeups"]) == 1


@pytest.mark.asyncio
async def test_one_shot_reminder_cannot_continue(monkeypatch: pytest.MonkeyPatch) -> None:
    task = SimpleNamespace(
        task_ref="reminder_once",
        mode="once",
        timezone="UTC",
        revision=1,
        delivery_target_type="channel",
        delivery_channel_id="channel-1",
    )

    async def owned(_owner, _ref):
        return task

    monkeypatch.setattr(tool, "_owned_task", owned)
    access = _access(trigger_kind="scheduled", reminder_task_ref=task.task_ref)
    owner = tool.BoundReminderOwner(
        access, "test", "bot-1", 1, "channel-1", "unified-user", "platform-user", "", "", False
    )
    request = tool.ResolveReminder(action="resolve", decision="continue", schedule=_schedule(), timezone="UTC")
    with pytest.raises(tool.DeliveryRejected, match="one-shot"):
        await tool._resolve(owner, request)
