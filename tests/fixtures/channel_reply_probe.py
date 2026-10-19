"""Exercise native reply extraction, receipts and record binding in an isolated runtime."""

from __future__ import annotations

import sys
import json
import asyncio
from pathlib import Path
from datetime import datetime

from creart import it
from satori import Text, User, Event, Login, Quote, Channel, EventType, ChannelType
from launart import Launart
from satori.model import MessageObject
from arclet.entari import Entari, Session, MessageChain
from satori.client import Account, ApiInfo
from arclet.letoderea import es
from arclet.entari.session import EntariProtocol
from arclet.letoderea.utils import set_event_loop
from arclet.entari.event.api import SendResponse
from arclet.entari.event.base import (
    MessageCreatedEvent,
    MessageDeletedEvent,
    MessageUpdatedEvent,
)
from arclet.entari.plugin.service import plugin_service
from graia.amnesia.builtins.aiohttp import AiohttpClientService


async def main() -> None:
    set_event_loop(asyncio.get_running_loop())
    root = Path(__file__).resolve().parents[2]
    config = {
        "basic": {
            "network": [],
            "external_dirs": [str(root / "plugins")],
            "log": {"level": "error", "rich_error": False},
        },
        "plugins": {
            "$prefix": [{"key": "", "plugins": ["channel_perception"]}],
            ".localdata": {"app_name": "reply-probe"},
            "database": {"type": "sqlite", "name": "probe.db", "create_table_at": "preparing"},
            "user": {},
            "channel_perception": {},
        },
    }
    Path("entari.json").write_text(json.dumps(config), encoding="utf-8")
    app = Entari.load("entari.json")
    manager = it(Launart)
    manager.add_component(AiohttpClientService())
    manager.add_component(app)
    from entari_plugin_database import Base, service as database

    # This message-path probe uses an existing schema, not concurrent first-start DDL.
    async with database.engines[""].begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    running = asyncio.create_task(manager.launch())
    account = Account(Login(platform="onebot", user=User("bot", name="Bot")), ApiInfo(), [], EntariProtocol)

    def event(kind, message_id: str, content: MessageChain):
        event_type, native_type = {
            "create": (EventType.MESSAGE_CREATED, MessageCreatedEvent),
            "update": (EventType.MESSAGE_UPDATED, MessageUpdatedEvent),
            "delete": (EventType.MESSAGE_DELETED, MessageDeletedEvent),
        }[kind]
        return native_type(
            account,
            Event(
                event_type,
                datetime.now(),
                account.self_info,
                channel=Channel("group", ChannelType.TEXT),
                user=User("member", name="Bot"),
                message=MessageObject(message_id, str(content)),
            ),
        )

    try:
        await asyncio.wait_for(plugin_service.status.wait_for("blocking"), 15)
        service = manager.get_component("channel_perception.service")
        await asyncio.wait_for(service.status.wait_for("blocking"), 15)
        await asyncio.wait_for(app.status.wait_for("blocking"), 15)
        source = Session(account, event("create", "source", MessageChain("Initial request")))
        await es.publish(
            SendResponse(
                account,
                "group",
                MessageChain("Combined request must not replace individual receipts"),
                [MessageObject("first", "First bubble"), MessageObject("second", "Second bubble")],
                source,
            )
        )
        incoming = event(
            "create", "reply", MessageChain([Quote("second", content=[Text("Second bubble")]), Text("Current request")])
        )
        assert not incoming.content.has(Quote)
        await es.publish(incoming)
        await service.flush()
        session = Session(account, incoming)
        page, _ = await service.recent_messages(session, limit=20)
        by_content = {row["content"]: row for row in page}
        assert {"First bubble", "Second bubble", "Current request"} <= by_content.keys(), page
        assert by_content["Current request"]["reply_to_cursor"] == by_content["Second bubble"]["cursor"]
        assert by_content["Current request"]["reply_to_status"] == "available"
        assert by_content["Current request"]["image_count"] == 0

        from plugins.llm_chat.reply_record import bind_reply_record
        from plugins.llm_chat.reply_context import collect_reply_context

        warnings = []
        bound = await bind_reply_record(
            session, collect_reply_context(session), warn=warnings.append, get_perception=lambda: service
        )
        assert bound["speaker_role"] == "assistant", bound
        assert bound["record_status"] == "matched"
        assert bound["content"] == "Second bubble"
        assert "image_ref" not in json.dumps(bound)

        updated = event(
            "update", "reply", MessageChain([Quote("first", content=[Text("First bubble")]), Text("Updated request")])
        )
        await es.publish(updated)
        await service.flush()
        page, _ = await service.recent_messages(session, limit=20)
        by_content = {row["content"]: row for row in page}
        assert by_content["Updated request"]["reply_to_cursor"] == by_content["First bubble"]["cursor"]
        await es.publish(event("delete", "second", MessageChain()))
        await service.flush()
        assert await service.replied_message(session) is None

        await es.publish(
            SendResponse(account, "other-group", MessageChain("Foreign"), [MessageObject("foreign", "Foreign")], source)
        )
        await es.publish(
            SendResponse(
                account,
                "group",
                MessageChain("Uncertain aggregate"),
                [MessageObject("unknown-a", ""), MessageObject("unknown-b", "")],
                source,
            )
        )
        await es.publish(SendResponse(account, "group", MessageChain("No receipt"), [], source))
        await service.flush()
        page, _ = await service.recent_messages(session, limit=20)
        assert all(row["content"] not in {"Foreign", "Uncertain aggregate", "No receipt"} for row in page)
        assert sum(row["content"] == "[Message unavailable]" for row in page) == 2
        assert warnings == [], warnings
        sys.stdout.write(
            json.dumps(
                {
                    "native_quote_retained": True,
                    "individual_receipts": ["First bubble", "Second bubble"],
                    "exact_self_reply": True,
                    "reply_update_retained": True,
                    "deleted_target_blocked": True,
                    "cross_channel_and_missing_content_safe": True,
                }
            )
            + "\n"
        )
    finally:
        await account.protocol.session.close()
        manager.status.exiting = True
        if group := manager.task_group:
            group.stop = True
            if group.blocking_task is not None:
                group.blocking_task.cancel()
        await asyncio.wait_for(running, 15)


if __name__ == "__main__":
    asyncio.run(main())
