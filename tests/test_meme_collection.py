"""Meme collection persistence and runtime integration tests."""

from __future__ import annotations

import sys
import json
from uuid import uuid4
from types import ModuleType, SimpleNamespace
import base64
from typing import Any, cast
import asyncio
import inspect
from pathlib import Path
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib.util import module_from_spec, spec_from_file_location
from collections.abc import Callable, AsyncIterator
from importlib.machinery import ModuleSpec

import pytest
import litellm
from sqlalchemy import func, select
from arclet.entari import Image, Session, MessageChain
from arclet.alconna import command_manager
from arclet.entari.const import ITEM_SESSION, ITEM_MESSAGE_CONTENT
from arclet.entari.config import EntariConfig
from arclet.entari.command import _commands
from arclet.entari.event.command import CommandExecute

if not hasattr(EntariConfig, "instance"):
    setattr(EntariConfig, "instance", EntariConfig.load(Path(__file__).resolve().parents[1] / "entari.yml"))
from satori import Text, Message
from arclet.letoderea import STOP, EVENT, Contexts
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
import entari_plugin_llm.service as llm_service_module
from arclet.entari.plugin.model import Plugin, current_plugin
from entari_plugin_llm.tools.event import tools, available_functions
import arclet.entari.command.provider as command_provider_module

import plugins as _PLUGINS

_PACKAGE = ModuleType("plugins.llm_chat")
_PACKAGE.__path__ = [str(Path(__file__).resolve().parents[1] / "plugins" / "llm_chat")]  # type: ignore[attr-defined]
sys.modules.setdefault("plugins.llm_chat", _PACKAGE)
setattr(_PLUGINS, "llm_chat", _PACKAGE)
if not hasattr(EntariConfig, "instance"):
    setattr(EntariConfig, "instance", EntariConfig.load(Path(__file__).resolve().parents[1] / "entari.yml"))

from entari_plugin_database import Base

from plugins.llm_chat import (
    generation as generation_module,
    image_tags as image_tags_module,
    meme_store as meme_store_module,
)
from plugins.llm_chat.config import LLMChatConfig
from plugins.llm_chat.models import ImageTag, Conversation
from plugins.llm_chat.vision import IMAGE_FETCH_MAX_BYTES
from plugins.llm_chat.persona import store as persona_store_module
from plugins.llm_chat.core.media import RECENT_MEME_HISTORY_NOTE, format_meme_collection_record
from plugins.llm_chat.meme_store import MemeImportError, MemeImportResult, import_meme_image
from plugins.llm_chat.web_access import DEFAULT_WEB_ACCESS_LIMITS
from plugins.llm_chat.chat_context import build_chat_messages
from plugins.llm_chat.core.delivery import DeliveryState, llm_chat_delivery_scope

sys.modules.pop("plugins.llm_chat", None)
if getattr(_PLUGINS, "llm_chat", None) is _PACKAGE:
    delattr(_PLUGINS, "llm_chat")

_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)
_GIF_BYTES = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==")
_SUPPORTED_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


class _MemeSession:
    def __init__(self, downloads: dict[str, bytes | BaseException]) -> None:
        self.downloads = downloads

    async def download(self, src: str) -> bytes:
        result = self.downloads[src]
        if isinstance(result, BaseException):
            raise result
        return result


async def _image_rows(session_factory: async_sessionmaker[Any]) -> list[ImageTag]:
    async with session_factory() as session:
        return list((await session.execute(select(ImageTag).order_by(ImageTag.file_path))).scalars())


def _stored_images(meme_dir: Path) -> list[Path]:
    return sorted(path for path in meme_dir.iterdir() if path.suffix.lower() in _SUPPORTED_SUFFIXES)


@pytest.fixture
async def meme_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    image_dir = tmp_path / "resources" / "image"
    meme_dir = image_dir / "memes"
    meme_dir.mkdir(parents=True)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    state = SimpleNamespace(
        tag_result="reaction，sticker，happy",
        embedding_result=[1.0, 0.0, 0.0],
        visual_calls=0,
        embedding_calls=[],
    )

    async def fake_generate_image_tags(_config: LLMChatConfig, _data_url: str) -> str:
        state.visual_calls += 1
        return cast(str, state.tag_result)

    async def fake_embed_text(_config: object, text: str) -> list[float] | None:
        state.embedding_calls.append(text)
        return cast(list[float] | None, state.embedding_result)

    monkeypatch.setattr(meme_store_module, "IMAGE_DIR", image_dir)
    monkeypatch.setattr(meme_store_module, "MEME_DIR", meme_dir)
    monkeypatch.setattr(meme_store_module, "generate_image_tags", fake_generate_image_tags)
    monkeypatch.setattr(meme_store_module, "_indexed_root", None)
    monkeypatch.setattr(meme_store_module, "_digest_paths", {})
    monkeypatch.setattr(meme_store_module, "_import_lock", asyncio.Lock())
    monkeypatch.setattr(image_tags_module, "get_session", session_factory)
    monkeypatch.setattr(image_tags_module, "embed_text", fake_embed_text)
    monkeypatch.setattr(image_tags_module, "_image_tag_lock", asyncio.Lock())
    image_tags_module._image_vectors.clear()

    try:
        yield SimpleNamespace(
            image_dir=image_dir,
            meme_dir=meme_dir,
            engine=engine,
            session_factory=session_factory,
            state=state,
            config=LLMChatConfig(),
        )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_import_uses_next_numeric_name_and_persists_queryable_tags(meme_env: Any) -> None:
    suffixes = [".jpg", ".jpeg", ".png", ".webp"]
    for stem in range(1, 63):
        suffix = suffixes[(stem - 1) % len(suffixes)]
        (meme_env.meme_dir / f"{stem}{suffix}").write_bytes(f"existing-{stem}".encode())

    data = _PNG_BYTES + b"new"
    result = await import_meme_image(
        meme_env.config,
        cast(Session, _MemeSession({"local://new": data})),
        Image.of(url="local://new"),
    )

    expected_path = str(Path("memes") / "63.png")
    assert result.status == "created"
    assert result.relative_path == expected_path
    assert result.tags == meme_env.state.tag_result
    assert (meme_env.meme_dir / "63.png").read_bytes() == data

    rows = await _image_rows(meme_env.session_factory)
    assert len(rows) == 1
    assert rows[0].file_path == expected_path
    assert rows[0].tags == meme_env.state.tag_result
    assert json.loads(rows[0].embedding_json) == meme_env.state.embedding_result


@pytest.mark.asyncio
async def test_duplicate_bytes_skip_repeated_tagging(meme_env: Any) -> None:
    data = _PNG_BYTES + b"duplicate"
    first = await import_meme_image(
        meme_env.config,
        cast(Session, _MemeSession({"local://first": data})),
        Image.of(url="local://first"),
    )
    second = await import_meme_image(
        meme_env.config,
        cast(Session, _MemeSession({"local://second": data})),
        Image.of(url="local://second"),
    )

    assert first.status == "created"
    assert second.status == "duplicate"
    assert second.relative_path == first.relative_path
    assert len(_stored_images(meme_env.meme_dir)) == 1
    assert len(await _image_rows(meme_env.session_factory)) == 1
    assert meme_env.state.visual_calls == 1


@pytest.mark.asyncio
async def test_existing_untagged_file_is_repaired_without_replacing_embedding(meme_env: Any) -> None:
    repair_data = _PNG_BYTES + b"repair"
    repair_path = meme_env.meme_dir / "7.png"
    repair_path.write_bytes(repair_data)
    relative_path = str(Path("memes") / "7.png")
    async with meme_env.session_factory() as session:
        session.add(ImageTag(file_path=relative_path, tags="", embedding_json="preserved"))
        await session.commit()

    meme_env.state.embedding_result = None
    repaired = await import_meme_image(
        meme_env.config,
        cast(Session, _MemeSession({"local://repair": repair_data})),
        Image.of(url="local://repair"),
    )

    assert repaired.status == "tagged_existing"
    assert repaired.relative_path == relative_path
    rows = await _image_rows(meme_env.session_factory)
    assert rows[0].tags == meme_env.state.tag_result
    assert rows[0].embedding_json == "preserved"
    assert len(_stored_images(meme_env.meme_dir)) == 1
    assert meme_env.state.visual_calls == 1


@pytest.mark.asyncio
async def test_concurrent_imports_deduplicate_and_allocate_consecutive_names(meme_env: Any) -> None:
    shared = _PNG_BYTES + b"shared"
    shared_session = cast(
        Session,
        _MemeSession({"local://shared-a": shared, "local://shared-b": shared}),
    )
    shared_results = await asyncio.gather(
        import_meme_image(meme_env.config, shared_session, Image.of(url="local://shared-a")),
        import_meme_image(meme_env.config, shared_session, Image.of(url="local://shared-b")),
    )

    assert {result.status for result in shared_results} == {"created", "duplicate"}
    assert {result.relative_path for result in shared_results} == {str(Path("memes") / "1.png")}
    assert meme_env.state.visual_calls == 1

    first_data = _PNG_BYTES + b"first-distinct"
    second_data = _PNG_BYTES + b"second-distinct"
    distinct_session = cast(
        Session,
        _MemeSession({"local://first": first_data, "local://second": second_data}),
    )
    distinct_results = await asyncio.gather(
        import_meme_image(meme_env.config, distinct_session, Image.of(url="local://first")),
        import_meme_image(meme_env.config, distinct_session, Image.of(url="local://second")),
    )

    assert {result.relative_path for result in distinct_results} == {
        str(Path("memes") / "2.png"),
        str(Path("memes") / "3.png"),
    }
    assert all(result.status == "created" for result in distinct_results)
    assert meme_env.state.visual_calls == 3


@pytest.mark.asyncio
async def test_invalid_inputs_and_empty_tags_leave_storage_unchanged(meme_env: Any) -> None:
    oversized = b"0" * (IMAGE_FETCH_MAX_BYTES + 1)
    oversized_src = "base64://" + base64.b64encode(oversized).decode("ascii")
    cases = [
        (
            cast(Session, _MemeSession({"local://gif": _GIF_BYTES})),
            Image.of(url="local://gif"),
            "Unsupported image format",
        ),
        (
            cast(Session, _MemeSession({"local://invalid": b"not image"})),
            Image.of(url="local://invalid"),
            "could not be recognized",
        ),
        (cast(Session, _MemeSession({})), Image.of(url="base64://!!!!"), "unavailable, invalid, or too large"),
        (cast(Session, _MemeSession({})), Image.of(url=oversized_src), "unavailable, invalid, or too large"),
        (
            cast(Session, _MemeSession({"local://failure": RuntimeError("secret download URL")})),
            Image.of(url="local://failure"),
            "unavailable, invalid, or too large",
        ),
    ]

    for session, image, message in cases:
        with pytest.raises(MemeImportError, match=message):
            await import_meme_image(meme_env.config, session, image)

    meme_env.state.tag_result = ""
    with pytest.raises(MemeImportError, match="returned no tags"):
        await import_meme_image(
            meme_env.config,
            cast(Session, _MemeSession({"local://empty": _PNG_BYTES})),
            Image.of(url="local://empty"),
        )

    assert _stored_images(meme_env.meme_dir) == []
    assert await _image_rows(meme_env.session_factory) == []


@pytest.mark.asyncio
async def test_initialization_cleans_stale_temp_and_deleted_cache_entry_is_rebuilt(meme_env: Any) -> None:
    stale = meme_env.meme_dir / ".meme-stale.tmp"
    stale.write_bytes(b"partial")
    data = _PNG_BYTES + b"cache"
    session = cast(Session, _MemeSession({"local://cache": data}))

    first = await import_meme_image(meme_env.config, session, Image.of(url="local://cache"))
    assert not stale.exists()
    first_path = meme_env.image_dir / first.relative_path
    first_path.unlink()

    second = await import_meme_image(meme_env.config, session, Image.of(url="local://cache"))

    assert second.status == "created"
    assert (meme_env.image_dir / second.relative_path).read_bytes() == data
    assert meme_env.state.visual_calls == 2


@pytest.mark.asyncio
async def test_temp_write_link_and_commit_failures_leave_no_artifacts(
    meme_env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = cast(Session, _MemeSession({"local://failure": _PNG_BYTES + b"failure"}))
    image = Image.of(url="local://failure")

    def fail_write(_path: Path, _data: bytes) -> None:
        raise OSError("write failed at absolute path")

    with monkeypatch.context() as patch:
        patch.setattr(meme_store_module, "_write_temp_file", fail_write)
        with pytest.raises(MemeImportError, match="Meme storage failed"):
            await import_meme_image(meme_env.config, session, image)
    assert list(meme_env.meme_dir.iterdir()) == []
    assert await _image_rows(meme_env.session_factory) == []

    def fail_link(_source: Path, _target: Path) -> None:
        raise OSError("link failed at absolute path")

    with monkeypatch.context() as patch:
        patch.setattr(meme_store_module.os, "link", fail_link)
        with pytest.raises(MemeImportError, match="Meme storage failed"):
            await import_meme_image(meme_env.config, session, image)
    assert list(meme_env.meme_dir.iterdir()) == []
    assert await _image_rows(meme_env.session_factory) == []

    async def fail_upsert(_config: LLMChatConfig, _path: str, _tags: str) -> None:
        raise RuntimeError("database commit failed")

    with monkeypatch.context() as patch:
        patch.setattr(meme_store_module, "upsert_image_tag", fail_upsert)
        with pytest.raises(MemeImportError, match="Image tag persistence failed"):
            await import_meme_image(meme_env.config, session, image)
    assert list(meme_env.meme_dir.iterdir()) == []
    assert await _image_rows(meme_env.session_factory) == []


@pytest.mark.asyncio
async def test_hard_link_collision_never_overwrites_external_file(
    meme_env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_link = meme_store_module.os.link
    collided = False

    def racing_link(source: Path, target: Path) -> None:
        nonlocal collided
        if not collided:
            collided = True
            target.write_bytes(b"external-writer")
            raise FileExistsError(target)
        original_link(source, target)

    monkeypatch.setattr(meme_store_module.os, "link", racing_link)
    data = _PNG_BYTES + b"collision"
    result = await import_meme_image(
        meme_env.config,
        cast(Session, _MemeSession({"local://collision": data})),
        Image.of(url="local://collision"),
    )

    assert result.relative_path == str(Path("memes") / "2.png")
    assert (meme_env.meme_dir / "1.png").read_bytes() == b"external-writer"
    assert (meme_env.meme_dir / "2.png").read_bytes() == data


@pytest.mark.asyncio
async def test_cancellation_waits_for_successful_tag_commit_and_preserves_duplicate(
    meme_env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_upsert = meme_store_module.upsert_image_tag
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking_upsert(config: LLMChatConfig, relative_path: str, tags: str) -> None:
        started.set()
        await release.wait()
        await original_upsert(config, relative_path, tags)

    monkeypatch.setattr(meme_store_module, "upsert_image_tag", blocking_upsert)
    data = _PNG_BYTES + b"cancel-success"
    session = cast(Session, _MemeSession({"local://cancel": data}))
    importer = asyncio.create_task(import_meme_image(meme_env.config, session, Image.of(url="local://cancel")))
    await started.wait()
    importer.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await importer

    files = _stored_images(meme_env.meme_dir)
    rows = await _image_rows(meme_env.session_factory)
    assert len(files) == len(rows) == 1

    duplicate = await import_meme_image(meme_env.config, session, Image.of(url="local://cancel"))
    assert duplicate.status == "duplicate"
    assert len(_stored_images(meme_env.meme_dir)) == 1


@pytest.mark.asyncio
async def test_cancellation_waits_for_failed_tag_commit_and_removes_file(
    meme_env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def failing_upsert(_config: LLMChatConfig, _relative_path: str, _tags: str) -> None:
        started.set()
        await release.wait()
        raise RuntimeError("commit failed")

    monkeypatch.setattr(meme_store_module, "upsert_image_tag", failing_upsert)
    data = _PNG_BYTES + b"cancel-failure"
    importer = asyncio.create_task(
        import_meme_image(
            meme_env.config,
            cast(Session, _MemeSession({"local://cancel": data})),
            Image.of(url="local://cancel"),
        )
    )
    await started.wait()
    importer.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await importer

    assert list(meme_env.meme_dir.iterdir()) == []
    async with meme_env.session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(ImageTag))
    assert count == 0


@dataclass(frozen=True)
class _RuntimeRegistrySnapshot:
    tool_schemas: tuple[dict[str, Any], ...]
    tool_functions: dict[str, Any]
    command_ids: tuple[int, ...]
    trie_items: tuple[tuple[str, tuple[str, ...]], ...]
    subscriber_ids: frozenset[str]


@dataclass
class _RuntimeHarness:
    plugin: Plugin
    module: ModuleType
    snapshot: _RuntimeRegistrySnapshot
    disposed: bool = False

    async def dispose(self) -> None:
        if self.disposed:
            return
        pending = self.plugin.dispose()
        if pending:
            current_loop = asyncio.get_running_loop()
            local = [task for task in pending if task.get_loop() is current_loop]
            for task in pending:
                if task.get_loop() is not current_loop:
                    task.cancel()
            if local:
                await asyncio.gather(*local, return_exceptions=True)
        self.disposed = True


_ROOT = Path(__file__).resolve().parents[1]
_LLM_CHAT_DIR = _ROOT / "plugins" / "llm_chat"
_MEME_RUNTIME_PATH = _LLM_CHAT_DIR / "meme_runtime.py"
_TOOL_RUNTIME_PATH = _LLM_CHAT_DIR / "tool_runtime.py"
_MISSING = object()


def _runtime_snapshot() -> _RuntimeRegistrySnapshot:
    return _RuntimeRegistrySnapshot(
        tool_schemas=tuple(tools),
        tool_functions=dict(available_functions),
        command_ids=tuple(id(item) for item in command_manager.get_commands()),
        trie_items=tuple((key, tuple(value)) for key, value in _commands.trie.items()),
        subscriber_ids=frozenset(_commands.subscribers),
    )


def _assert_runtime_snapshot(snapshot: _RuntimeRegistrySnapshot) -> None:
    assert len(tools) == len(snapshot.tool_schemas)
    assert all(current is expected for current, expected in zip(tools, snapshot.tool_schemas, strict=True))
    assert available_functions.keys() == snapshot.tool_functions.keys()
    assert all(available_functions[name] is value for name, value in snapshot.tool_functions.items())
    assert tuple(id(item) for item in command_manager.get_commands()) == snapshot.command_ids
    assert tuple((key, tuple(value)) for key, value in _commands.trie.items()) == snapshot.trie_items
    assert frozenset(_commands.subscribers) == snapshot.subscriber_ids


def _tool_schema_delta(snapshot: _RuntimeRegistrySnapshot) -> list[dict[str, Any]]:
    previous = {id(schema) for schema in snapshot.tool_schemas}
    return [schema for schema in tools if id(schema) not in previous]


def _callable(module: ModuleType, name: str) -> Callable[..., Any]:
    registered = getattr(module, name)
    return cast(Callable[..., Any], getattr(registered, "callable_target", registered))


@asynccontextmanager
async def _temporary_plugin(module_path: Path) -> AsyncIterator[_RuntimeHarness]:
    prefix = "plugins.llm_chat"
    previous_package = sys.modules.get(prefix)
    previous_package_attr = getattr(_PLUGINS, "llm_chat", _MISSING)
    package = previous_package
    if package is None:
        package = ModuleType(prefix)
        package.__package__ = prefix
        package.__path__ = [str(_LLM_CHAT_DIR)]  # type: ignore[attr-defined]
        package.__spec__ = ModuleSpec(prefix, loader=None, is_package=True)
        if package.__spec__.submodule_search_locations is not None:
            package.__spec__.submodule_search_locations.append(str(_LLM_CHAT_DIR))
        sys.modules[prefix] = package
    setattr(_PLUGINS, "llm_chat", package)

    snapshot = _runtime_snapshot()
    module_name = f"plugins.llm_chat._meme_runtime_test_{uuid4().hex}"
    spec = spec_from_file_location(module_name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = module_from_spec(spec)
    sys.modules[module_name] = module
    plugin: Plugin | None = None
    token: Any = None
    harness: _RuntimeHarness | None = None
    try:
        plugin = Plugin(module_name, module, config={})
        setattr(module, "__plugin__", plugin)
        token = current_plugin.set(plugin)
        harness = _RuntimeHarness(plugin=plugin, module=module, snapshot=snapshot)
        spec.loader.exec_module(module)
        yield harness
    finally:
        if token is not None:
            current_plugin.reset(token)
        if harness is not None:
            await harness.dispose()
        elif plugin is not None:
            pending = plugin.dispose()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        sys.modules.pop(module_name, None)
        if previous_package is None:
            sys.modules.pop(prefix, None)
        if previous_package_attr is _MISSING:
            if getattr(_PLUGINS, "llm_chat", _MISSING) is package:
                delattr(_PLUGINS, "llm_chat")
        else:
            setattr(_PLUGINS, "llm_chat", previous_package_attr)


class _RuntimeSession(Session[Any]):
    def __init__(
        self,
        *,
        direct: list[Any] | None = None,
        quoted: list[Any] | None = None,
        downloads: dict[str, bytes | BaseException] | None = None,
    ) -> None:
        self.elements = MessageChain(direct or [])
        self._quote = None if quoted is None else SimpleNamespace(children=quoted)
        self.reply = None if quoted is None else SimpleNamespace(origin=SimpleNamespace(message=MessageChain(quoted)))
        self.downloads = downloads or {}
        channel = SimpleNamespace(id="channel")
        self.account = SimpleNamespace(platform="test")
        self.event = SimpleNamespace(user=SimpleNamespace(id="user"), channel=channel)
        self.sent: list[Any] = []

    @property
    def quote(self) -> Any:
        return self._quote

    async def download(self, src: str) -> bytes:
        result = self.downloads[src]
        if isinstance(result, BaseException):
            raise result
        return result

    async def send(self, message: Any, *_args: Any, **_kwargs: Any) -> list[Any]:
        self.sent.append(message)
        return []


async def _execute_registered_command(
    message: str | MessageChain,
    session: Session,
) -> str | MessageChain | None:
    async def ignore_event(_event: object) -> None:
        return None

    chain = MessageChain(message)
    context = Contexts()
    context[EVENT] = CommandExecute(chain, session)
    context[ITEM_MESSAGE_CONTENT] = chain
    context[ITEM_SESSION] = session
    original_post = command_provider_module.post
    command_provider_module.post = ignore_event
    try:
        result = await _commands.execute(chain, context)
    finally:
        command_provider_module.post = original_post
    if result is None:
        return None
    return cast(str | MessageChain | None, result.args[0])


@pytest.mark.asyncio
async def test_tag_image_schema_scope_indexing_and_privacy(monkeypatch: pytest.MonkeyPatch) -> None:
    async with _temporary_plugin(_MEME_RUNTIME_PATH) as harness:
        schemas = _tool_schema_delta(harness.snapshot)
        assert harness.module.registered_tools == ["tag_image"]
        assert [schema["function"]["name"] for schema in schemas] == ["tag_image"]
        schema = schemas[0]["function"]
        assert schema["parameters"]["required"] == []
        assert schema["parameters"]["properties"]["image_index"]["type"] == "integer"
        description = schema["description"]
        normalized_description = " ".join(description.split())
        assert "current direct or replied images" in normalized_description
        assert "1-based" in normalized_description
        assert "direct images first" in normalized_description
        assert "bare unavailable markers" in normalized_description
        assert "ordinary or sensitive images" in normalized_description
        assert "forwarded messages" in normalized_description

        target = _callable(harness.module, "tag_image")
        assert inspect.signature(target).parameters["image_index"].default == 1
        direct = Image.of(url="local://direct")
        quoted = Image.of(url="local://quoted")
        session = cast(Session, _RuntimeSession(direct=[direct], quoted=[quoted]))
        imported: list[Image] = []

        async def fake_import(_config: LLMChatConfig, _session: Session, image: Image) -> MemeImportResult:
            imported.append(image)
            return MemeImportResult("created", "memes/secret.png", "secret，tags")

        monkeypatch.setattr(harness.module, "import_meme_image", fake_import)

        with pytest.raises(MemeImportError, match="outside an active"):
            await target(session)

        with llm_chat_delivery_scope(DeliveryState()):
            result = await target(session, 2)
        assert imported == [quoted]
        assert "Collected" in result
        assert "secret" not in result
        assert "memes/" not in result

        with llm_chat_delivery_scope(DeliveryState()):
            with pytest.raises(MemeImportError, match="positive 1-based"):
                await target(session, 0)
            with pytest.raises(MemeImportError, match="does not identify"):
                await target(session, 3)

        forwarded = cast(
            Session,
            _RuntimeSession(direct=[Message(forward=True, content=[Image.of(url="local://forwarded")])]),
        )
        with llm_chat_delivery_scope(DeliveryState()):
            with pytest.raises(MemeImportError, match="does not identify"):
                await target(forwarded)


@pytest.mark.asyncio
async def test_tag_meme_command_parses_reply_and_direct_image_and_rejects_ambiguous_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _temporary_plugin(_MEME_RUNTIME_PATH) as harness:
        imported: list[Image] = []

        async def allow(_session: Session) -> None:
            return None

        async def fake_import(_config: LLMChatConfig, _session: Session, image: Image) -> MemeImportResult:
            imported.append(image)
            return MemeImportResult("created", "memes/1.png", "reaction，happy")

        monkeypatch.setattr(harness.module, "_superuser_check", allow)
        monkeypatch.setattr(harness.module, "import_meme_image", fake_import)

        replied = Image.of(url="local://replied")
        reply_session = cast(Session, _RuntimeSession(quoted=[replied]))
        reply_result = await _execute_registered_command("llmchat tag-meme", reply_session)
        assert isinstance(reply_result, str)
        assert reply_result
        assert imported[-1] is replied

        direct = Image.of(url="local://direct")
        ignored_reply = Image.of(url="local://ignored-reply")
        direct_session = cast(Session, _RuntimeSession(quoted=[ignored_reply]))
        direct_result = await _execute_registered_command(
            MessageChain([Text("llmchat tag-meme "), direct]),
            direct_session,
        )
        assert isinstance(direct_result, str)
        assert direct_result
        assert imported[-1] is direct

        calls_before = len(imported)
        for message, session in [
            (
                MessageChain([Text("llmchat tag-meme "), Image.of(url="local://one"), Image.of(url="local://two")]),
                cast(Session, _RuntimeSession()),
            ),
            (MessageChain([Text("llmchat tag-meme manual-tag")]), cast(Session, _RuntimeSession())),
            (
                MessageChain([Text("llmchat tag-meme "), Message(forward=True, content=[])]),
                cast(Session, _RuntimeSession()),
            ),
            ("llmchat tag-meme", cast(Session, _RuntimeSession(quoted=[replied, ignored_reply]))),
        ]:
            result = await _execute_registered_command(message, session)
            assert isinstance(result, str)
            assert result
        assert len(imported) == calls_before

        assert await _execute_registered_command("llmchat tag-images", cast(Session, _RuntimeSession())) is None
        assert await _execute_registered_command("llmchat retag-images", cast(Session, _RuntimeSession())) is None


@pytest.mark.asyncio
async def test_tag_meme_command_claims_permission_and_failure_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _temporary_plugin(_MEME_RUNTIME_PATH) as harness:
        image = Image.of(url="local://image")
        session = cast(Session, _RuntimeSession(direct=[image]))

        async def deny(_session: Session) -> object:
            return STOP

        async def forbidden_import(*_args: Any, **_kwargs: Any) -> MemeImportResult:
            raise AssertionError("unauthorized command must not import")

        monkeypatch.setattr(harness.module, "_superuser_check", deny)
        monkeypatch.setattr(harness.module, "import_meme_image", forbidden_import)
        denied = await _execute_registered_command(MessageChain([Text("llmchat tag-meme "), image]), session)
        assert isinstance(denied, str)
        assert "Permission denied" in denied

        async def allow(_session: Session) -> None:
            return None

        current_result = MemeImportResult("created", "memes/1.png", "reaction，happy")

        async def status_import(*_args: Any, **_kwargs: Any) -> MemeImportResult:
            return current_result

        monkeypatch.setattr(harness.module, "_superuser_check", allow)
        monkeypatch.setattr(harness.module, "import_meme_image", status_import)
        for current_result, expected_status in [
            (MemeImportResult("created", "memes/1.png", "reaction，happy"), "Collected"),
            (MemeImportResult("duplicate", "memes/1.png", "reaction，happy"), "Already collected"),
            (MemeImportResult("tagged_existing", "memes/1.png", "reaction，happy"), "Tagged existing image"),
        ]:
            response = await _execute_registered_command(
                MessageChain([Text("llmchat tag-meme "), image]),
                session,
            )
            assert isinstance(response, str)
            assert expected_status in response
            assert "memes/1.png" in response
            assert "reaction，happy" in response

        async def fail_import(*_args: Any, **_kwargs: Any) -> MemeImportResult:
            raise MemeImportError("safe failure")

        monkeypatch.setattr(harness.module, "_superuser_check", allow)
        monkeypatch.setattr(harness.module, "import_meme_image", fail_import)
        failed = await _execute_registered_command(MessageChain([Text("llmchat tag-meme "), image]), session)
        assert isinstance(failed, str)
        assert "safe failure" in failed

        no_image = await _execute_registered_command("llmchat tag-meme", cast(Session, _RuntimeSession()))
        assert isinstance(no_image, str)
        assert no_image


def _tool_call(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments, ensure_ascii=False),
        },
    }


def _model_response(
    content: str | None = None,
    *,
    tool_calls: list[dict[str, Any]] | None = None,
) -> litellm.ModelResponse:
    return litellm.ModelResponse(
        model="test-model",
        choices=[
            {
                "index": 0,
                "finish_reason": "tool_calls" if tool_calls else "stop",
                "message": {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": tool_calls,
                },
            }
        ],
    )


def _install_completion_script(
    monkeypatch: pytest.MonkeyPatch,
    script: list[litellm.ModelResponse],
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    index = 0

    async def scripted_acompletion(**payload: Any) -> litellm.ModelResponse:
        nonlocal index
        payloads.append(payload)
        if index >= len(script):
            raise AssertionError("unexpected extra completion round")
        response = script[index]
        index += 1
        return response

    monkeypatch.setattr(llm_service_module.litellm, "acompletion", scripted_acompletion)
    monkeypatch.setattr(
        llm_service_module,
        "get_model_config",
        lambda _model=None: SimpleNamespace(
            name="test-model",
            prompt="",
            base_url="https://llm.invalid/v1",
            api_key="fake-llm-key",
            extra={},
        ),
    )
    return payloads


@pytest.mark.asyncio
async def test_scripted_collection_send_and_duplicate_command_smoke(
    meme_env: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    meme_env.state.tag_result = "reaction，happy，sticker"
    meme_env.state.embedding_result = None
    data = _PNG_BYTES + b"scripted-smoke"
    image = Image.of(url="local://smoke")
    session = _RuntimeSession(direct=[image], downloads={"local://smoke": data})
    payloads = _install_completion_script(
        monkeypatch,
        [
            _model_response(tool_calls=[_tool_call("tag-1", "tag_image", {"image_index": 1})]),
            _model_response("Visible collection reply"),
        ],
    )
    monkeypatch.setattr(persona_store_module, "get_session", meme_env.session_factory)

    async with _temporary_plugin(_MEME_RUNTIME_PATH) as meme_runtime:
        async with _temporary_plugin(_TOOL_RUNTIME_PATH) as tool_runtime:
            monkeypatch.setattr(tool_runtime.module, "IMAGE_DIR", meme_env.image_dir)
            monkeypatch.setattr(tool_runtime.module, "get_session", meme_env.session_factory)

            context = Contexts()
            context[ITEM_SESSION] = session
            response = await generation_module.generate_chat_response(
                [{"role": "user", "content": "collect this image"}],
                system="meme smoke system",
                model="test-model",
                channel_id="channel",
                ctx=context,
                web_limits=DEFAULT_WEB_ACCESS_LIMITS,
                delivery_state=DeliveryState(),
            )

            final_text = response.choices[0].message.content
            assert final_text == "Visible collection reply"
            await session.send(final_text)
            await persona_store_module.append_message("channel", "", "bot", "assistant", final_text)

            relative_path = str(Path("memes") / "1.png")
            collection_record = format_meme_collection_record(relative_path, meme_env.state.tag_result)
            files = _stored_images(meme_env.meme_dir)
            rows = await _image_rows(meme_env.session_factory)
            assert len(files) == len(rows) == 1
            assert rows[0].file_path == relative_path
            tool_messages = [message for message in payloads[1]["messages"] if message["role"] == "tool"]
            assert len(tool_messages) == 1
            assert "memes/1.png" not in tool_messages[0]["content"]
            assert meme_env.state.tag_result not in tool_messages[0]["content"]

            async with meme_env.session_factory() as database:
                history = list(
                    (
                        await database.execute(
                            select(Conversation).where(Conversation.channel_id == "channel").order_by(Conversation.id)
                        )
                    ).scalars()
                )
            assert [row.content for row in history] == [collection_record, "Visible collection reply"]
            assert await persona_store_module.load_latest_meme_collection("channel") == (
                relative_path,
                meme_env.state.tag_result,
            )
            next_turn_messages = build_chat_messages(history, "Alice", "send it")
            assistant_context = [message["content"] for message in next_turn_messages if message["role"] == "assistant"]
            assert RECENT_MEME_HISTORY_NOTE in assistant_context
            rendered_context = json.dumps(next_turn_messages, ensure_ascii=False)
            assert relative_path not in rendered_context
            assert meme_env.state.tag_result not in rendered_context

            distractor_path = str(Path("memes") / "2.png")
            (meme_env.meme_dir / "2.png").write_bytes(_PNG_BYTES + b"distractor")
            async with meme_env.session_factory() as database:
                database.add(ImageTag(file_path=distractor_path, tags="reaction，happy，sticker", embedding_json=""))
                await database.commit()

            send_image = _callable(tool_runtime.module, "send_image")
            with llm_chat_delivery_scope(DeliveryState()):
                send_result = await send_image(session, "recently collected", True)
            assert send_result.startswith("已发送图片")
            sent_chain = cast(MessageChain, session.sent[-1])
            assert sent_chain.get(Image)[0].src.replace("\\", "/").endswith("/1.png")

            with llm_chat_delivery_scope(DeliveryState()):
                explicit_result = await send_image(session, r"please send memes\2.png", False)
            assert explicit_result.startswith("已发送图片")
            explicit_chain = cast(MessageChain, session.sent[-1])
            assert explicit_chain.get(Image)[0].src.replace("\\", "/").endswith("/2.png")

            async with meme_env.session_factory() as database:
                sent_history = list(
                    (
                        await database.execute(
                            select(Conversation).where(Conversation.channel_id == "channel").order_by(Conversation.id)
                        )
                    ).scalars()
                )
            assert [row.content for row in sent_history] == [
                collection_record,
                "Visible collection reply",
                "[发送了表情包: reaction，happy，sticker]",
                "[发送了表情包: reaction，happy，sticker]",
            ]

            async def allow(_session: Session) -> None:
                return None

            monkeypatch.setattr(meme_runtime.module, "_superuser_check", allow)
            duplicate = await _execute_registered_command(
                MessageChain([Text("llmchat tag-meme "), image]),
                cast(Session, session),
            )
            assert isinstance(duplicate, str)
            assert "Already collected" in duplicate
            assert len(_stored_images(meme_env.meme_dir)) == 2
            assert len(await _image_rows(meme_env.session_factory)) == 2


@pytest.mark.asyncio
async def test_meme_runtime_dispose_restores_tool_and_command_registries() -> None:
    snapshot = _runtime_snapshot()
    async with _temporary_plugin(_MEME_RUNTIME_PATH) as harness:
        assert _tool_schema_delta(snapshot)
        assert tuple(id(item) for item in command_manager.get_commands()) != snapshot.command_ids
        await harness.dispose()
        _assert_runtime_snapshot(snapshot)
    _assert_runtime_snapshot(snapshot)
