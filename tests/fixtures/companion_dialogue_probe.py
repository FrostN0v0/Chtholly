"""Exercise real plugin routing, cooperative generation and native tool receipts."""

from __future__ import annotations

import os
import sys
import json
import time
import asyncio
from pathlib import Path
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo
import importlib
from collections import Counter

from creart import it
from satori import At, Text, User, Event, Login, Channel, EventType, ChannelType
from launart import Launart, Service
import litellm
from sqlalchemy import select
from satori.const import Api
from satori.model import MessageObject
from arclet.entari import Entari, Session, MessageChain
from satori.client import Account, ApiInfo
from arclet.letoderea import es
from arclet.entari.session import EntariProtocol
from arclet.letoderea.utils import set_event_loop
from arclet.entari.event.base import MessageCreatedEvent
from arclet.entari.plugin.service import plugin_service
from graia.amnesia.builtins.aiohttp import AiohttpClientService

from utils.companion_core import LifeState, ContactProposal, ReflectionProposal
from utils.relationship_core.models import AXIS_KEYS


def response(model, *, content=None, calls=()):
    return litellm.ModelResponse(
        model=model,
        choices=[
            {
                "index": 0,
                "finish_reason": "tool_calls" if calls else "stop",
                "message": {"role": "assistant", "content": content, "tool_calls": list(calls) or None},
            }
        ],
        usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    )


def call(name, arguments, identifier):
    return {"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def current_inputs(payload):
    result = []
    for message in payload["messages"]:
        content = message.get("content")
        if message["role"] != "user" or not isinstance(content, str):
            continue
        try:
            value = json.loads(content)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("speaker_id"):
            result.append(value)
    return result


async def main():
    set_event_loop(asyncio.get_running_loop())
    root = Path(__file__).resolve().parents[2]
    hold = os.environ.get("COMPANION_PROBE_HOLD") == "1"
    port = int(os.environ.get("COMPANION_PROBE_PORT", "0"))
    started = time.monotonic()
    wall_started = datetime.now(UTC)
    clock_offset = timedelta()

    def clock():
        return wall_started + clock_offset + timedelta(seconds=time.monotonic() - started)

    config = {
        "basic": {
            "network": [],
            "external_dirs": [str(root / "plugins")],
            "prefix": ["/"],
            "log": {"level": "warning", "rich_error": False},
        },
        "plugins": {
            "$prefix": [{"key": "", "plugins": ["channel_perception", "llm_chat"]}],
            ".localdata": {"app_name": "companion-dialogue-probe"},
            ".scheduler": {},
            "database": {"type": "sqlite", "name": "probe.db", "create_table_at": "preparing"},
            "server": {"host": "127.0.0.1", "port": port, "path": "satori", "adapters": []},
            "entari_plugin_webui": {"password": "synthetic-probe-password"},
            "user": {},
            "channel_perception": {},
            "llm": {
                "api_key": "synthetic-probe-key",
                "base_url": "https://provider.invalid/v1",
                "models": [
                    {"name": f"openai/{name}-probe", "alias": name} for name in ("dialogue", "reflection", "evaluation")
                ],
            },
            "llm_chat": {
                "$filter": "channel.type is public",
                "model": "dialogue",
                "eval_model": "evaluation",
                "companion_model": "reflection",
                "memory_enabled": False,
                "image_tags_enabled": False,
                "web_search_enabled": False,
                "relationship_eval_debounce_seconds": 10,
                "default_persona": "alpha",
                "personas": {"alpha": {"name": "Alpha", "prompt": "Alpha enjoys friendly, concise conversation."}},
            },
        },
    }
    phase = "normal"
    counts = Counter()
    requests = []
    wire = []
    entered, release = asyncio.Event(), asyncio.Event()
    clock_module = None
    original_datetime = None

    async def completion(**payload):
        model = payload["model"]
        if model == "openai/reflection-probe":
            proposal = ReflectionProposal(life=LifeState(), decision_summary="No unsolicited contact is needed now.")
            if phase == "host":
                proposal = ReflectionProposal(
                    life=LifeState(),
                    contact=ContactProposal(
                        target="channel",
                        member_ref=None,
                        key="painting",
                        purpose="share",
                        topic="Share a painting idea",
                        reason="An original group conversation opening",
                        evidence_turn_ids=[],
                    ),
                    decision_summary="Offer a shared painting topic.",
                )
            return response(model, content=proposal.model_dump_json())
        if model == "openai/evaluation-probe":
            episodes = json.loads(payload["messages"][-1]["content"])["episodes"]
            return response(
                model,
                content=json.dumps(
                    {
                        "deltas": dict.fromkeys(AXIS_KEYS, 0),
                        "description": "Observed a complete conversation.",
                        "impression": "A conversation partner",
                        "emotions": [],
                        "processed_turn_ids": [item["turn_id"] for item in episodes],
                        "profile_patches": [],
                        "memory_items": [],
                    }
                ),
            )
        assert model == "openai/dialogue-probe", model
        counts[phase] += 1
        expected_calls = 2 if phase == "group" else 1
        assert counts[phase] <= expected_calls, (
            phase,
            [
                {
                    "role": message["role"],
                    "tool_call_id": message.get("tool_call_id"),
                    "content": str(message.get("content", ""))[:2000] if message["role"] == "tool" else "",
                    "tools": [item.get("function", {}).get("name") for item in message.get("tool_calls", [])],
                }
                for message in payload["messages"][-10:]
            ],
        )
        requests.append((phase, payload))
        names = {item["function"]["name"] for item in payload.get("tools", [])}
        assert ("companion_dialogue" in names) == (phase != "normal"), names
        if phase == "group" and counts[phase] == 1:
            admitted = current_inputs(payload)
            assert {item["content"] for item in admitted} == {"Probe alpha", "Probe beta"}, admitted
            assert len({item["speaker_id"] for item in admitted}) == 2
            entered.set()
            await release.wait()
            return response(
                model, calls=[call("send_msg", {"segments": [{"type": "text", "text": "Stale candidate"}]}, "stale")]
            )
        text = {
            "normal": "Normal reply",
            "group": "Companion reply",
            "private": "Private reply",
            "host": "Shared painting idea",
            "followup": "Continuing the shared thought",
        }[phase]
        if phase == "host":
            assert current_inputs(payload) == []
        if phase == "followup":
            assert any(
                message["role"] == "assistant" and "Shared painting idea" in str(message.get("content"))
                for message in payload["messages"]
            ), payload["messages"]
        calls = [call("send_msg", {"segments": [{"type": "text", "text": text}]}, f"send-{phase}")]
        if phase != "normal":
            inputs = current_inputs(payload)
            if phase == "followup":
                inputs = inputs[-1:]
            if phase == "group":
                assert {item["content"] for item in inputs} == {"Probe alpha", "Probe beta", "Probe correction"}, inputs
                assert len({item["speaker_id"] for item in inputs}) == 3
            calls.append(
                call(
                    "companion_dialogue",
                    {
                        "action": "end",
                        "engaged_speakers": list(dict.fromkeys(item["speaker_id"] for item in inputs)),
                    },
                    f"control-{phase}",
                )
            )
        calls.append(
            call(
                "finish_turn",
                {"outcome": "delivered", "reason": "The intended response is confirmed."},
                f"finish-{phase}",
            )
        )
        return response(model, calls=calls)

    litellm.acompletion = completion
    Path("entari.json").write_text(json.dumps(config), encoding="utf-8")
    app = Entari.load("entari.json")
    manager = it(Launart)
    manager.add_component(AiohttpClientService())
    manager.add_component(app)
    from entari_plugin_database import Base, service as database, get_session

    # The probe exercises an existing isolated schema, not concurrent first-start DDL.
    async with database.engines[""].begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    account = Account(
        Login(platform="test", adapter="test", user=User("bot", name="Bot")), ApiInfo(), [], EntariProtocol
    )
    account.connected.set()
    app.accounts["companion-probe"] = account
    channels = {
        "normal": Channel("normal", ChannelType.TEXT),
        "group": Channel("group", ChannelType.TEXT),
        "private": Channel("private", ChannelType.DIRECT),
    }

    async def call_api(action, params):
        if action == Api.CHANNEL_GET:
            channel = channels[params["channel_id"]]
            return {"id": channel.id, "type": channel.type.value}
        if action == Api.MESSAGE_CREATE:
            wire.append(dict(params))
            return [{"id": f"receipt-{len(wire)}", "content": params["content"]}]
        raise AssertionError(f"Unexpected external action: {action}")

    account.protocol.call_api = call_api

    def event(channel, user, identifier, text, *, directed=False):
        elements = [Text(text)]
        if directed:
            elements.insert(0, At("bot"))
        return MessageCreatedEvent(
            account,
            Event(
                EventType.MESSAGE_CREATED,
                datetime.now(UTC),
                account.self_info,
                channel=channels[channel],
                user=User(user, name="Shared display name"),
                message=MessageObject(identifier, str(MessageChain(elements))),
            ),
        )

    async def scenario():
        nonlocal phase, clock_module, original_datetime, clock_offset
        await asyncio.wait_for(plugin_service.status.wait_for("blocking"), 30)
        await asyncio.wait_for(app.status.wait_for("blocking"), 30)
        companion = manager.get_component("llm_chat.companion.service")
        await asyncio.wait_for(companion.status.wait_for("blocking"), 30)
        from entari_plugin_user.utils import get_user

        # Exercise existing members; concurrent first-enrollment DDL/DML is not this probe's contract.
        for user_id in ("normal-user", "alpha", "beta", "gamma", "peer"):
            await get_user("test", User(user_id, name="Shared display name"))
        package = type(companion).__module__.rsplit(".", 2)[0]
        session_manager = importlib.import_module(f"{package}.session_manager")
        models = importlib.import_module(f"{package}.models")
        store = importlib.import_module(f"{package}.companion.store")
        personality = importlib.import_module(f"{package}.personality")
        clock_module = importlib.import_module(type(companion).__module__)
        original_datetime = clock_module.datetime

        class ProbeDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                current = clock()
                return current.astimezone(tz) if tz is not None else current.replace(tzinfo=None)

        # Replace only the external clock, not the registered service or its owner.
        clock_module.datetime = ProbeDateTime

        async def enable(channel):
            native = Session(account, event(channel, "alpha", f"setup-{channel}", "Setup"))
            scope = await session_manager.get_or_create_scope(session_manager.scope_identity(native))
            await personality.select_scope_persona(companion.config, scope.id, "alpha")
            await store.set_mode(scope.id, "companion", persona_key="alpha", now=clock())
            return scope

        async def finish(scope):
            async with asyncio.timeout(40):
                while companion.dialogues.active(scope.id):
                    await asyncio.sleep(0.01)

        await es.publish(event("normal", "normal-user", "normal-ignored", "Ignored ambient input"))
        await es.publish(event("normal", "normal-user", "normal-directed", "Addressed input", directed=True))
        assert counts["normal"] == 1, (counts, wire)
        assert len(wire) == 1, (counts, wire)

        phase = "group"
        scope = await enable("group")
        await es.publish(event("group", "alpha", "group-a", "Probe alpha"))
        await es.publish(event("group", "beta", "group-b", "Probe beta"))
        await asyncio.wait_for(entered.wait(), 30)
        await es.publish(event("group", "gamma", "group-c", "Probe correction", directed=True))
        release.set()
        await finish(scope)
        assert counts["group"] == 2, (counts, wire)
        assert len(wire) == 2, (counts, wire)
        assert "Stale candidate" not in json.dumps(wire)
        assert "Companion reply" in wire[-1]["content"]

        phase = "private"
        private_scope = await enable("private")
        await es.publish(event("private", "peer", "private-a", "Private input"))
        await finish(private_scope)
        assert counts["private"] == 1, (counts, wire)
        assert len(wire) == 3, (counts, wire)
        assert wire[-1]["channel_id"] == "private"
        assert "Private reply" in wire[-1]["content"]
        await companion.drain()

        phase = "host"
        await store.set_mode(private_scope.id, "normal", persona_key="alpha", now=clock())
        host_scope = scope
        wake_at = (clock() + timedelta(minutes=6)).astimezone(ZoneInfo("Asia/Shanghai"))
        if wake_at.hour >= 23:
            wake_at = (wake_at + timedelta(days=1)).replace(hour=7, minute=1)
        elif wake_at.hour < 7:
            wake_at = wake_at.replace(hour=7, minute=1)
        clock_offset += wake_at.astimezone(UTC) - clock()
        await companion.claim_ready()
        await asyncio.wait_for(companion.drain(), 30)
        assert counts["host"] == 1, (counts, wire)
        assert len(wire) == 4, wire
        contact = (await store.read_contact_history(host_scope.id, "alpha"))[0]
        assert contact["status"] == "delivered"
        async with get_session() as db:
            host_turn = await db.get(models.AgentTurn, contact["turn_id"])
            assert host_turn.trigger_kind == "autonomous"
            assert host_turn.user_id == ""

        phase = "followup"
        await es.publish(event("group", "peer", "host-followup", "That painting idea sounds good"))
        await finish(host_scope)
        assert counts["followup"] == 1, (counts, wire)
        assert len(wire) == 5, (counts, wire)
        async with get_session() as db:
            turns = list(
                (await db.scalars(select(models.AgentTurn).where(models.AgentTurn.trigger_kind == "message"))).all()
            )
            assert {turn.trigger_message_id for turn in turns} == {
                "normal-directed",
                "group-a",
                "private-a",
                "host-followup",
            }, turns
            assert all(turn.status == "completed" for turn in turns), [
                (turn.trigger_message_id, turn.status) for turn in turns
            ]
            group_turn = next(turn for turn in turns if turn.trigger_message_id == "group-a")
            events = list(
                (await db.scalars(select(models.AgentEvent).where(models.AgentEvent.turn_id == group_turn.id))).all()
            )
            sources = [
                json.loads(item.payload_json)["host_author"] for item in events if item.event_type == "user_input"
            ]
            assert {item["source_message_id"] for item in sources} == {"group-a", "group-b", "group-c"}, sources
            assert len({item["user_id"] for item in sources}) == 3
            assert len([item for item in events if item.event_type == "assistant_output"]) == 1
        sys.stdout.write(
            json.dumps(
                {
                    "normal_addressed_only": True,
                    "group_single_owner": True,
                    "cross_member_steering": True,
                    "stale_candidate_sent": False,
                    "private_native_route": True,
                    "host_to_live_history": True,
                    "real_native_receipts": len(wire),
                    "distinct_group_authors": len(sources),
                }
            )
            + "\n"
        )
        sys.stdout.flush()
        if hold:
            sys.stdout.write("NATIVE_COMPANION_READY\n")
            sys.stdout.flush()
            await asyncio.Event().wait()

    failures = []

    class ProbeService(Service):
        id = "companion.probe"
        required = {"entari.service"}
        stages = {"blocking"}

        async def launch(self, manager):
            async with self.stage("blocking"):
                try:
                    await scenario()
                except Exception as exc:
                    failures.append(exc)
                finally:
                    release.set()
                    if clock_module is not None:
                        clock_module.datetime = original_datetime
                    manager.status.exiting = True
                    if group := manager.task_group:
                        group.stop = True
                        if group.blocking_task is not None:
                            group.blocking_task.cancel()

    manager.add_component(ProbeService())
    try:
        async with asyncio.timeout(None if hold else 100):
            await manager.launch()
    finally:
        account.connected.clear()
        await account.protocol.session.close()
    if failures:
        raise failures[0]


if __name__ == "__main__":
    asyncio.run(main())
