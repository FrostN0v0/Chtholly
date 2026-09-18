"""Structured mentions and closed channel-reply capability regressions."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import cast
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text, event, select, update
from arclet.entari import At, Text, Image, Session, ChannelType
from test_web_tools_runtime import local_modules as local_modules
from test_channel_perception import (
    AmbientMessage,
    MessageMutation,
    MessageObservation,
    ChannelPerceptionConfig,
    _scope,
    core_module,
    _participant,
    queries_module,
    service_module,
    perception_store as perception_store,
    message_store_module,
    participant_store_module,
)

from plugins.llm_chat.channel_message_refs import (
    ChannelMessageReferences,
    ChannelMessageReferenceError,
    channel_message_scope,
)


def _session(*, account="bot-1", channel="group-1") -> Session:
    return cast(
        Session,
        SimpleNamespace(
            account=SimpleNamespace(platform="onebot", self_id=account),
            channel=SimpleNamespace(id=channel, type=ChannelType.TEXT),
        ),
    )


async def _record(scope, message_id, elements, *, when=None, command=False):
    when = when or datetime.utcnow()
    await message_store_module.store_observation(
        MessageObservation(
            kind="message",
            participant=_participant(scope, card="Same", observed_at=when),
            message_id=message_id,
            message=core_module.normalize_message(elements, max_chars=2000),
            display_name="Same",
            directed_to_bot=False,
            is_command=command,
            is_bot=False,
            observed_at=when,
        ),
        ChannelPerceptionConfig(),
    )


def test_history_image_sources_preserve_unavailable_original_positions():
    assert core_module.collect_image_sources([Image(src=""), Image(src="https://example.com/second.png")]) == [
        "",
        "https://example.com/second.png",
    ]


@pytest.mark.asyncio
async def test_mentions_follow_native_identity_not_labels_and_clear_on_mutation(perception_store):
    scope = _scope()
    now = datetime.utcnow()
    for member in ("first", "second"):
        await participant_store_module.upsert_participant(
            _participant(scope, card="Same", user_id=member, observed_at=now)
        )
    await _record(
        scope,
        "mentions",
        [
            At(id="first", name="Same"),
            At(id="second"),
            At(id="first", name="Same"),
            Text("@Same"),
            At.all(),
            At.role_("moderators", "Mods"),
            At(id="bot-1"),
            At(id="unobserved", name="Same"),
        ],
    )
    messages, _ = await queries_module.get_recent_messages(scope, limit=5)
    mentions = messages[0]["mentions"]
    assert [item["position"] for item in mentions] == list(range(7))
    assert mentions[0]["participant_ref"] == mentions[2]["participant_ref"]
    assert mentions[1]["participant_ref"] != mentions[0]["participant_ref"]
    assert mentions[1]["display_name"] == ""
    assert [item["kind"] for item in mentions[3:6]] == ["all", "role", "bot"]
    assert mentions[6]["status"] == "unknown"
    assert "participant_ref" not in mentions[6]
    assert all("target_id" not in item for item in mentions)
    cursor = messages[0]["cursor"]
    await _record(scope, "mentions", [Text("@Same is text now")])
    assert (await queries_module.get_exact_message(scope, cursor))["mentions"] == []
    await message_store_module.store_observation(
        MessageMutation("message_delete", scope, "mentions", None, datetime.utcnow()),
        ChannelPerceptionConfig(),
    )
    assert await queries_module.get_exact_message(scope, cursor) is None
    async with perception_store.session_factory() as db:
        row = (await db.execute(select(AmbientMessage))).scalar_one()
        assert row.content == ""
        assert row.mentions_json is None


@pytest.mark.asyncio
async def test_message_schema_migration_is_additive_idempotent_and_legacy_unknown(perception_store, monkeypatch):
    migration = service_module.initialize_message_store
    monkeypatch.setitem(migration.__globals__, "get_session", perception_store.session_factory)
    scope = _scope()
    await _record(scope, "legacy", [Text("@Same")])
    async with perception_store.session_factory() as db:
        await db.execute(text("ALTER TABLE channel_perception_messages DROP COLUMN mentions_json"))
        await db.commit()
    await migration()
    await migration()
    messages, _ = await queries_module.get_recent_messages(scope, limit=5)
    assert messages[0]["mentions"] is None
    assert messages[0]["content"] == "@Same"
    await _record(scope, "new", [Text("No native mentions")])
    messages, _ = await queries_module.get_recent_messages(scope, limit=5)
    assert messages[-1]["mentions"] == []
    async with perception_store.engine.begin() as connection:
        await connection.run_sync(lambda conn: AmbientMessage.__table__.drop(conn))
    await migration()
    await migration()
    await _record(scope, "fresh", [At(id="bot-1")])
    messages, _ = await queries_module.get_recent_messages(scope, limit=5)
    assert messages[0]["mentions"][0]["kind"] == "bot"


@pytest.mark.asyncio
async def test_reply_lookup_is_batched_and_exact_reads_recheck_visibility(perception_store):
    from arclet.entari import Quote

    scope = _scope()
    await _record(scope, "target", [Text("original")])
    for index in range(8):
        await _record(scope, f"reply-{index}", [Quote(id="target"), Text(str(index))])
    statements = []

    def count(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(perception_store.engine.sync_engine, "before_cursor_execute", count)
    try:
        page, _ = await queries_module.get_recent_messages(scope, limit=8)
    finally:
        event.remove(perception_store.engine.sync_engine, "before_cursor_execute", count)
    assert len(statements) == 2
    target = page[0]["reply_to_cursor"]
    assert {row["reply_to_status"] for row in page} == {"outside_page"}
    assert {row["reply_to_cursor"] for row in page} == {target}
    assert (await queries_module.get_exact_message(scope, target))["content"] == "original"
    assert await queries_module.get_exact_message(_scope(account_id="other"), target) is None
    narrow = ChannelPerceptionConfig(max_messages_per_channel=8)
    assert await queries_module.get_exact_message(scope, target, config=narrow) is None
    narrow_page, _ = await queries_module.get_recent_messages(scope, limit=8, config=narrow)
    assert all(row["reply_to_status"] == "unavailable" for row in narrow_page)
    for values in (
        {"is_command": True},
        {"is_command": False, "deleted_at": datetime.utcnow()},
        {"deleted_at": None, "created_at": datetime.utcnow() - timedelta(days=31)},
    ):
        async with perception_store.session_factory() as db:
            await db.execute(update(AmbientMessage).where(AmbientMessage.id == int(target)).values(**values))
            await db.commit()
        assert await queries_module.get_exact_message(scope, target) is None
        assert await queries_module.get_message_image_target(scope, target) is None
        page, _ = await queries_module.get_recent_messages(scope, limit=8)
        assert all(row["reply_to_status"] == "unavailable" and not row["reply_to_cursor"] for row in page)
    await message_store_module.prune_scope(scope, ChannelPerceptionConfig(), datetime.utcnow())
    async with perception_store.session_factory() as db:
        assert await db.get(AmbientMessage, int(target)) is None
    service = service_module.ChannelPerceptionService(ChannelPerceptionConfig())
    private = _session()
    private.channel.type = ChannelType.DIRECT
    with pytest.raises(ValueError, match="private conversations"):
        await service.exact_message(private, target)


def test_message_refs_are_stable_scoped_and_separate_from_pages():
    session = _session()
    references = ChannelMessageReferences()
    with channel_message_scope(references):
        reference = references.register(session, "7")
        assert references.register(session, "7") == reference
        page = references.page(session, "7", "member")
        with pytest.raises(ChannelMessageReferenceError):
            references.resolve(session, page)
        with pytest.raises(ChannelMessageReferenceError):
            references.resolve_page(session, page, "other")
        assert references.resolve_page(session, page, "member") == "7"
        for other in (_session(account="other"), _session(channel="other")):
            with pytest.raises(ChannelMessageReferenceError):
                references.resolve(other, reference)
        with pytest.raises(ChannelMessageReferenceError):
            ChannelMessageReferences().resolve(session, reference)
    with pytest.raises(ChannelMessageReferenceError):
        references.resolve(session, reference)


def test_truncated_history_recomputes_reply_closure_and_hides_locators(local_modules):
    session = _session()
    references = ChannelMessageReferences()
    serializer = local_modules.history_tools._serialize_history_page
    messages = [
        {
            "cursor": str(index),
            "message_id": f"private-{index}",
            "content": "x" * 3000,
            "reply_to_cursor": "1" if index else "",
            "reply_to_status": "available" if index else "none",
        }
        for index in range(6)
    ]
    page = json.loads(serializer(messages, "", references=references, session=session))
    assert page["truncated"] is True
    assert all(row["reply_to_status"] == "outside_page" for row in page["messages"])
    assert all("cursor" not in row and "message_id" not in row for row in page["messages"])
    reply_ref = page["messages"][-1]["reply_to_ref"]
    exact = json.loads(serializer([messages[1]], "", references=references, session=session, exact=True))
    assert exact["messages"][0]["message_ref"] == reply_ref
    assert exact["messages"][0]["reply_to_status"] == "available"
    assert references.resolve_page(session, page["next_cursor"]) == references.resolve(
        session, page["messages"][0]["message_ref"]
    )
