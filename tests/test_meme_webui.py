"""Meme administration catalog and WebUI API behavior tests."""

from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from arclet.entari.config import EntariConfig
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

if not hasattr(EntariConfig, "instance"):
    setattr(EntariConfig, "instance", EntariConfig.load(Path(__file__).resolve().parents[1] / "entari.yml"))

from entari_plugin_database import Base

from plugins.llm_chat.config import LLMChatConfig
from plugins.llm_chat.models import ImageTag
from plugins.llm_chat.meme_admin import MemeAdminError, MemeAdminService
from plugins.llm_chat.meme_store import MemeImportError, MemeDeleteResult, MemeImportResult
from plugins.llm_chat.meme_webui_api import create_meme_admin_router

_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010804000000b51c0c020000000b4944415478da63fcff1f0003030200efbfa7db0000000049454e44ae426082"
)
_GIF_BYTES = bytes.fromhex("47494638396101000100800000000000ffffff21f904000a0000002c00000000010001000002014c003b")


@pytest.fixture
async def admin_env(tmp_path: Path) -> AsyncIterator[SimpleNamespace]:
    image_dir = tmp_path / "resources" / "image"
    meme_dir = image_dir / "memes"
    meme_dir.mkdir(parents=True)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    state = SimpleNamespace(replacements=[], tag_calls=0, next_upload=10)

    async def replace_tags(config: LLMChatConfig, relative_path: str, tags: str) -> None:
        state.replacements.append((relative_path, tags))
        async with session_factory() as session:
            row = (
                await session.execute(select(ImageTag).where(ImageTag.file_path == relative_path))
            ).scalar_one_or_none()
            if row is None:
                session.add(ImageTag(file_path=relative_path, tags=tags, embedding_json=""))
            else:
                row.tags = tags
                row.embedding_json = ""
            await session.commit()

    async def import_bytes(
        config: LLMChatConfig,
        data: bytes,
        *,
        manual_tags: str | None = None,
        auto_tag: bool = True,
    ) -> MemeImportResult:
        assert data
        assert manual_tags is not None or auto_tag
        file_name = f"{state.next_upload}.png"
        state.next_upload += 1
        relative_path = f"memes/{file_name}"
        (meme_dir / file_name).write_bytes(data)
        tags = manual_tags or "auto，generated"
        async with session_factory() as session:
            session.add(ImageTag(file_path=relative_path, tags=tags, embedding_json="[]"))
            await session.commit()
        return MemeImportResult("created", relative_path, tags)

    async def delete_entry(relative_path: str) -> MemeDeleteResult:
        file_name = relative_path.replace("\\", "/").rsplit("/", 1)[-1]
        path = meme_dir / file_name
        file_deleted = path.exists()
        path.unlink(missing_ok=True)
        async with session_factory() as session:
            row = (
                await session.execute(select(ImageTag).where(ImageTag.file_path == relative_path))
            ).scalar_one_or_none()
            index_deleted = row is not None
            if row is not None:
                await session.delete(row)
                await session.commit()
        return MemeDeleteResult(file_deleted=file_deleted, index_deleted=index_deleted)

    async def generate_tags(config: LLMChatConfig, data_url: str) -> str:
        state.tag_calls += 1
        return "auto，generated，visible text"

    service = MemeAdminService(
        LLMChatConfig(),
        session_factory=session_factory,
        meme_dir=meme_dir,
        importer=import_bytes,
        tag_replacer=replace_tags,
        deleter=delete_entry,
        tagger=generate_tags,
    )
    try:
        yield SimpleNamespace(
            image_dir=image_dir,
            meme_dir=meme_dir,
            engine=engine,
            session_factory=session_factory,
            state=state,
            service=service,
        )
    finally:
        await engine.dispose()


async def _add_row(admin_env: SimpleNamespace, file_path: str, tags: str, embedding: str = "[]") -> None:
    async with admin_env.session_factory() as session:
        session.add(ImageTag(file_path=file_path, tags=tags, embedding_json=embedding))
        await session.commit()


@pytest.mark.asyncio
async def test_catalog_unions_stored_unindexed_and_missing_entries(admin_env: SimpleNamespace) -> None:
    (admin_env.meme_dir / "1.png").write_bytes(_PNG_BYTES)
    (admin_env.meme_dir / "2.gif").write_bytes(_GIF_BYTES)
    await _add_row(admin_env, "memes/1.png", "reaction，quoted text")
    await _add_row(admin_env, "memes/3.png", "missing file")

    page = await admin_env.service.list_memes(sort="name", page_size=10)

    assert [item.file_name for item in page.items] == ["1.png", "2.gif", "3.png"]
    assert [item.status for item in page.items] == ["indexed", "unindexed", "missing"]
    assert page.stats == {"stored": 2, "indexed": 1, "unindexed": 1, "missing": 1}
    assert page.items[0].to_dict()["tag_count"] == 2
    assert admin_env.service.resolve_file("../private.png") is None
    assert admin_env.service.resolve_file("\0.jpg") is None

    search = await admin_env.service.list_memes(query="quoted text", page_size=10)
    assert [item.file_name for item in search.items] == ["1.png"]
    missing = await admin_env.service.list_memes(status="missing", page_size=10)
    assert [item.file_name for item in missing.items] == ["3.png"]


@pytest.mark.asyncio
async def test_admin_service_updates_retags_uploads_and_deletes(admin_env: SimpleNamespace) -> None:
    (admin_env.meme_dir / "1.png").write_bytes(_PNG_BYTES)
    await _add_row(admin_env, "memes/1.png", "old tags", embedding="stale")

    updated = await admin_env.service.update_tags("1.png", "1. visible text; reaction，visible text")
    assert updated.tags == "visible text，reaction"
    assert updated.embedding_ready is False
    assert admin_env.state.replacements[-1] == ("memes/1.png", "visible text，reaction")

    retagged = await admin_env.service.retag("1.png")
    assert retagged.tags == "auto，generated，visible text"
    assert admin_env.state.tag_calls == 1

    uploaded = await admin_env.service.upload(_PNG_BYTES, tags="manual，entry", auto_tag=False)
    assert uploaded.status == "created"
    assert uploaded.item.file_name == "10.png"
    assert uploaded.item.tags == "manual，entry"

    deleted = await admin_env.service.delete("10.png")
    assert deleted == MemeDeleteResult(file_deleted=True, index_deleted=True)
    with pytest.raises(MemeAdminError, match="not found"):
        await admin_env.service.get_item("10.png")


@pytest.mark.asyncio
async def test_admin_service_maps_delete_storage_failure_to_server_error(admin_env: SimpleNamespace) -> None:
    (admin_env.meme_dir / "1.png").write_bytes(_PNG_BYTES)
    await _add_row(admin_env, "memes/1.png", "reaction")

    async def fail_delete(relative_path: str) -> MemeDeleteResult:
        raise MemeImportError(f"Meme file deletion failed: {relative_path}")

    service = MemeAdminService(
        LLMChatConfig(),
        session_factory=admin_env.session_factory,
        meme_dir=admin_env.meme_dir,
        deleter=fail_delete,
    )
    with pytest.raises(MemeAdminError) as raised:
        await service.delete("1.png")

    assert raised.value.code == "delete_failed"
    assert raised.value.status == 500
    assert (admin_env.meme_dir / "1.png").is_file()


@pytest.mark.asyncio
async def test_webui_router_serves_assets_and_mutation_contracts(admin_env: SimpleNamespace) -> None:
    (admin_env.meme_dir / "1.png").write_bytes(_PNG_BYTES)
    await _add_row(admin_env, "memes/1.png", "reaction，visible text")
    auth_calls = 0

    async def authorize() -> None:
        nonlocal auth_calls
        auth_calls += 1

    app = FastAPI()
    asset_dir = Path(__file__).resolve().parents[1] / "plugins" / "llm_chat" / "webui"
    app.include_router(
        create_meme_admin_router(
            admin_env.service,
            asset_dir=asset_dir,
            auth_dependency=authorize,
        )
    )
    transport = httpx.ASGITransport(app=app)

    async def oversized_tags() -> AsyncIterator[bytes]:
        yield b'{"tags":"'
        yield b"x" * 8200
        yield b'"}'

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        page = await client.get("/api/llm-chat/memes/page")
        catalog = await client.get("/api/llm-chat/memes")
        image = await client.get("/api/llm-chat/memes/files/1.png")
        updated = await client.patch(
            "/api/llm-chat/memes/1.png",
            headers={"X-Requested-With": "test"},
            json={"tags": "edited，visible text"},
        )
        uploaded = await client.post(
            "/api/llm-chat/memes",
            headers={"X-Requested-With": "test"},
            files={"file": ("new.png", _PNG_BYTES, "image/png")},
            data={"tags": "manual，upload", "auto_tag": "false"},
        )
        oversized = await client.patch(
            "/api/llm-chat/memes/1.png",
            headers={"X-Requested-With": "test"},
            content=oversized_tags(),
        )
        deleted = await client.delete(
            "/api/llm-chat/memes/1.png",
            headers={"X-Requested-With": "test"},
        )
        missing = await client.get("/api/llm-chat/memes/files/not-found.png")

    assert page.status_code == 200
    assert "Meme Library" in page.text
    assert "default-src 'self'" in page.headers["content-security-policy"]
    assert "blob:" in page.headers["content-security-policy"]
    first_item = catalog.json()["items"][0]
    assert first_item["image_url"].endswith("/files/1.png?v=" + str(first_item["version"]))
    assert "embedding_json" not in first_item
    assert str(admin_env.meme_dir) not in catalog.text
    assert image.content == _PNG_BYTES
    assert updated.json()["item"]["tags"] == "edited，visible text"
    assert uploaded.status_code == 201
    assert uploaded.json()["item"]["file_name"] == "10.png"
    assert deleted.json() == {"success": True, "file_deleted": True, "index_deleted": True}
    assert oversized.status_code == 413
    assert oversized.json()["code"] == "request_too_large"
    assert missing.status_code == 404
    assert auth_calls >= 7
