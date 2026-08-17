"""Channel-perception identity and message-lifecycle regression tests."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
import asyncio
from pathlib import Path
from datetime import datetime, timedelta
from importlib import import_module
from importlib.machinery import ModuleSpec

import pytest
from sqlalchemy import select
from arclet.entari import At, Text, Image, Quote, MessageChain
from arclet.entari.config import EntariConfig
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import plugins as plugins_package

_ROOT = Path(__file__).resolve().parents[1]
if not hasattr(EntariConfig, "instance"):
    EntariConfig.instance = EntariConfig.load(_ROOT / "entari.yml")

from entari_plugin_database import Base

_PACKAGE_NAME = "plugins.channel_perception"
_PACKAGE_DIR = _ROOT / "plugins" / "channel_perception"
_previous_package = sys.modules.get(_PACKAGE_NAME)
_previous_attribute = getattr(plugins_package, "channel_perception", None)
_test_package = ModuleType(_PACKAGE_NAME)
_test_package.__package__ = _PACKAGE_NAME
_test_package.__path__ = [str(_PACKAGE_DIR)]  # type: ignore[attr-defined]
_test_package.__spec__ = ModuleSpec(_PACKAGE_NAME, loader=None, is_package=True)
if _test_package.__spec__.submodule_search_locations is not None:
    _test_package.__spec__.submodule_search_locations.append(str(_PACKAGE_DIR))
sys.modules[_PACKAGE_NAME] = _test_package
setattr(plugins_package, "channel_perception", _test_package)

core_module = import_module(f"{_PACKAGE_NAME}.core")
models_module = import_module(f"{_PACKAGE_NAME}.models")
config_module = import_module(f"{_PACKAGE_NAME}.config")
schemas_module = import_module(f"{_PACKAGE_NAME}.schemas")
queries_module = import_module(f"{_PACKAGE_NAME}.queries")
message_store_module = import_module(f"{_PACKAGE_NAME}.message_store")
participant_store_module = import_module(f"{_PACKAGE_NAME}.participant_store")

for module_name in [name for name in sys.modules if name == _PACKAGE_NAME or name.startswith(f"{_PACKAGE_NAME}.")]:
    sys.modules.pop(module_name, None)
if _previous_package is not None:
    sys.modules[_PACKAGE_NAME] = _previous_package
if _previous_attribute is None:
    delattr(plugins_package, "channel_perception")
else:
    setattr(plugins_package, "channel_perception", _previous_attribute)

AmbientMessage = models_module.AmbientMessage
ChannelParticipant = models_module.ChannelParticipant
ChannelPerceptionConfig = config_module.ChannelPerceptionConfig
MessageMutation = schemas_module.MessageMutation
MessageObservation = schemas_module.MessageObservation
PerceptionScope = schemas_module.PerceptionScope
ParticipantObservation = schemas_module.ParticipantObservation
NormalizedMessage = core_module.NormalizedMessage


@pytest.fixture
async def perception_store(monkeypatch: pytest.MonkeyPatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    identity_id = {"value": 10}

    async def fake_get_user(_platform: str, _user: object) -> SimpleNamespace:
        return SimpleNamespace(id=identity_id["value"])

    monkeypatch.setattr(participant_store_module, "_UPSERT_LOCK", None)
    monkeypatch.setattr(participant_store_module, "get_session", session_factory)
    monkeypatch.setattr(participant_store_module, "get_user", fake_get_user)
    monkeypatch.setattr(message_store_module, "get_session", session_factory)
    monkeypatch.setattr(queries_module, "get_session", session_factory)
    try:
        yield SimpleNamespace(
            engine=engine,
            session_factory=session_factory,
            identity_id=identity_id,
        )
    finally:
        await engine.dispose()


def _scope(*, account_id: str = "bot-1", channel_id: str = "group-1"):
    return PerceptionScope(
        platform="onebot",
        account_id=account_id,
        guild_id=channel_id,
        channel_id=channel_id,
    )


def _participant(
    scope,
    *,
    card: str,
    nickname: str = "Alice",
    avatar_url: str = "https://q.qlogo.cn/example",
    user_id: str = "user-1",
    observed_at: datetime,
):
    return ParticipantObservation(
        scope=scope,
        platform_user_id=user_id,
        platform_nickname=nickname,
        group_card=card,
        avatar_url=avatar_url,
        observed_at=observed_at,
    )


def _message(content: str, *, reply_to: str = ""):
    return NormalizedMessage(
        content=content,
        reply_to_message_id=reply_to,
        image_count=0,
    )


def test_normalize_message_preserves_safe_structure_and_bounds() -> None:
    chain = MessageChain(
        [
            Text("  hello   there  "),
            At(id="user-2"),
            Image(src="https://example.com/image.png"),
            Quote(id="message-0", content=[Text("quoted secret")]),
        ]
    )

    normalized = core_module.normalize_message(chain, max_chars=24)

    assert normalized.content == "hello there @member [..."
    assert normalized.reply_to_message_id == "message-0"
    assert normalized.image_count == 1
    assert "https://" not in normalized.content
    assert "quoted secret" not in normalized.content
    assert core_module.display_name("Group Card", "Nickname", "fallback") == "Group Card"
    assert core_module.is_prefixed_command("  /help", ["/"], "Chtholly") is True
    assert core_module.is_prefixed_command("@Chtholly, hello", [], "Chtholly") is True


@pytest.mark.asyncio
async def test_identity_name_change_and_message_lifecycle_are_consistent(perception_store) -> None:
    scope = _scope()
    started = datetime(2026, 8, 17, 10, 0, 0)
    config = ChannelPerceptionConfig(retention_days=7, max_messages_per_channel=20)

    await message_store_module.store_observation(
        MessageObservation(
            kind="message",
            participant=_participant(scope, card="First Card", observed_at=started),
            message_id="message-1",
            message=_message("first text"),
            display_name="First Card",
            directed_to_bot=False,
            is_command=False,
            is_bot=False,
            observed_at=started,
        ),
        config,
    )
    first = await participant_store_module.find_participant_by_platform_user(scope, "user-1")
    assert first is not None

    perception_store.identity_id["value"] = 20
    changed_at = started + timedelta(minutes=1)
    await message_store_module.store_observation(
        MessageObservation(
            kind="message_update",
            participant=_participant(scope, card="Second Card", observed_at=changed_at),
            message_id="message-1",
            message=_message("edited text"),
            display_name="Second Card",
            directed_to_bot=False,
            is_command=False,
            is_bot=False,
            observed_at=changed_at,
        ),
        config,
    )
    second = await participant_store_module.find_participant_by_platform_user(scope, "user-1")
    assert second is not None
    assert second.public_ref == first.public_ref
    assert second.person_id == 20
    assert second.previous_person_ids == (10,)
    assert second.previous_names == ("First Card",)
    assert second.display_name == "Second Card"

    await message_store_module.store_observation(
        MessageMutation(
            kind="message_delete",
            scope=scope,
            message_id="message-1",
            message=None,
            observed_at=started + timedelta(minutes=2),
        ),
        config,
    )
    await message_store_module.store_observation(
        MessageObservation(
            kind="message",
            participant=_participant(scope, card="Second Card", observed_at=started + timedelta(minutes=3)),
            message_id="message-2",
            message=_message("current topic"),
            display_name="Second Card",
            directed_to_bot=False,
            is_command=False,
            is_bot=False,
            observed_at=started + timedelta(minutes=3),
        ),
        config,
    )

    messages, next_cursor = await queries_module.get_recent_messages(scope, limit=20)
    ambient = await queries_module.get_ambient_context(scope, max_messages=8, max_chars=4000)
    old_name_matches = await queries_module.find_participants(scope, "First Card", limit=5)

    assert next_cursor == ""
    assert [message["content"] for message in messages] == ["current topic"]
    assert [item["content"] for item in ambient] == ["current topic"]
    assert old_name_matches == [
        {
            "participant_ref": first.public_ref,
            "display_name": "Second Card",
            "platform_nickname": "Alice",
            "group_card": "Second Card",
            "last_seen_at": "2026-08-17T10:03:00Z",
            "avatar_available": True,
        }
    ]

    async with perception_store.session_factory() as session:
        deleted = (
            await session.execute(select(AmbientMessage).where(AmbientMessage.message_id == "message-1"))
        ).scalar_one()
        assert deleted.content == ""
        assert deleted.deleted_at == started + timedelta(minutes=2)


@pytest.mark.asyncio
async def test_participant_identity_uses_group_card_when_platform_nickname_is_missing(
    perception_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured_names: list[str | None] = []

    async def capture_get_user(_platform: str, platform_user: object) -> SimpleNamespace:
        name = getattr(platform_user, "name", None)
        captured_names.append(name if isinstance(name, str) else None)
        return SimpleNamespace(id=perception_store.identity_id["value"])

    monkeypatch.setattr(participant_store_module, "get_user", capture_get_user)
    participant = await participant_store_module.upsert_participant(
        _participant(
            _scope(),
            card="Group Card",
            nickname="",
            observed_at=datetime(2026, 8, 17, 10, 25, 0),
        )
    )

    assert captured_names == ["Group Card"]
    assert participant.display_name == "Group Card"


@pytest.mark.asyncio
async def test_concurrent_participant_upserts_are_idempotent(perception_store) -> None:
    scope = _scope()
    observed_at = datetime(2026, 8, 17, 10, 30, 0)
    observation = _participant(scope, card="Concurrent", observed_at=observed_at)

    first, second = await asyncio.gather(
        participant_store_module.upsert_participant(observation),
        participant_store_module.upsert_participant(observation),
    )

    async with perception_store.session_factory() as session:
        rows = list((await session.execute(select(ChannelParticipant))).scalars().all())

    assert first.public_ref == second.public_ref
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_avatar_cache_update_requires_current_url(perception_store) -> None:
    scope = _scope()
    started = datetime(2026, 8, 17, 10, 45, 0)
    first_url = "https://example.com/old.png"
    current_url = "https://example.com/current.png"
    participant = await participant_store_module.upsert_participant(
        _participant(scope, card="", nickname="", avatar_url=first_url, observed_at=started)
    )
    await participant_store_module.upsert_participant(
        _participant(
            scope,
            card="",
            nickname="",
            avatar_url=current_url,
            observed_at=started + timedelta(minutes=1),
        )
    )
    assert participant.display_name == "member"

    await participant_store_module.update_avatar_observation(
        scope,
        participant.public_ref,
        expected_avatar_url=first_url,
        avatar_hash="stale",
        avatar_description="stale description",
        observed_at=started + timedelta(minutes=2),
    )
    stale = await participant_store_module.find_participant_by_platform_user(scope, "user-1")
    assert stale is not None
    assert stale.avatar_url == current_url
    assert stale.avatar_description == ""

    await participant_store_module.update_avatar_observation(
        scope,
        participant.public_ref,
        expected_avatar_url=current_url,
        avatar_hash="current",
        avatar_description="current description",
        observed_at=started + timedelta(minutes=3),
    )
    current = await participant_store_module.find_participant_by_platform_user(scope, "user-1")
    assert current is not None
    assert current.avatar_description == "current description"


@pytest.mark.asyncio
async def test_scope_isolation_and_retention_limit(perception_store) -> None:
    started = datetime(2026, 8, 17, 11, 0, 0)
    primary = _scope(account_id="bot-1")
    other_account = _scope(account_id="bot-2")
    config = ChannelPerceptionConfig(retention_days=7, max_messages_per_channel=2)

    for index in range(3):
        observed_at = started + timedelta(minutes=index)
        await message_store_module.store_observation(
            MessageObservation(
                kind="message",
                participant=_participant(primary, card="Primary", observed_at=observed_at),
                message_id=f"primary-{index}",
                message=_message(f"primary text {index}"),
                display_name="Primary",
                directed_to_bot=False,
                is_command=False,
                is_bot=False,
                observed_at=observed_at,
            ),
            config,
        )
    await message_store_module.store_observation(
        MessageObservation(
            kind="message",
            participant=_participant(other_account, card="Other", observed_at=started),
            message_id="other-0",
            message=_message("other account text"),
            display_name="Other",
            directed_to_bot=False,
            is_command=False,
            is_bot=False,
            observed_at=started,
        ),
        config,
    )

    primary_messages, _ = await queries_module.get_recent_messages(primary, limit=20)
    other_messages, _ = await queries_module.get_recent_messages(other_account, limit=20)

    assert [message["content"] for message in primary_messages] == ["primary text 1", "primary text 2"]
    assert [message["content"] for message in other_messages] == ["other account text"]


@pytest.mark.asyncio
async def test_participant_retention_is_bounded(perception_store) -> None:
    scope = _scope()
    started = datetime(2026, 8, 17, 12, 0, 0)
    config = ChannelPerceptionConfig(
        participant_retention_days=1,
        max_participants_per_channel=2,
        retention_days=1,
    )

    await message_store_module.store_observation(
        _participant(
            scope,
            card="Old",
            user_id="user-old",
            observed_at=started - timedelta(days=2),
        ),
        config,
    )
    for index in range(3):
        await message_store_module.store_observation(
            _participant(
                scope,
                card=f"Member {index}",
                user_id=f"user-{index}",
                observed_at=started + timedelta(minutes=index),
            ),
            config,
        )

    async with perception_store.session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(ChannelParticipant)
                    .where(
                        ChannelParticipant.platform == scope.platform,
                        ChannelParticipant.account_id == scope.account_id,
                        ChannelParticipant.channel_id == scope.channel_id,
                    )
                    .order_by(ChannelParticipant.last_seen_at)
                )
            )
            .scalars()
            .all()
        )

    assert [row.platform_user_id for row in rows] == ["user-1", "user-2"]
