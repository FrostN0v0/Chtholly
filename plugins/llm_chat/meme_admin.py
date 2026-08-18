"""Administrative mutations for the managed meme collection."""

from __future__ import annotations

from typing import Protocol
import asyncio
from pathlib import Path
from dataclasses import dataclass
from collections.abc import Awaitable

from arclet.entari.logger import log
from entari_plugin_database import get_session

from utils.path import MEME_DIR

from .config import LLMChatConfig
from .vision import generate_image_tags
from .image_tags import replace_image_tags
from .meme_store import (
    MemeImportError,
    MemeDeleteResult,
    MemeImportResult,
    delete_meme,
    import_meme_bytes,
)
from .meme_catalog import (
    MemeCatalog,
    SessionFactory,
    MemeCatalogItem,
    MemeCatalogPage,
    MemeCatalogSort,
    MemeCatalogError,
    MemeCatalogFilter,
)
from .core.image_source import image_file_to_data_url
from .core.image_tag_metadata import normalize_generated_image_tags

_LOGGER = log.wrapper("[llm_chat]")


class MemeBytesImporter(Protocol):
    def __call__(
        self,
        config: LLMChatConfig,
        data: bytes,
        *,
        manual_tags: str | None = None,
        auto_tag: bool = True,
    ) -> Awaitable[MemeImportResult]: ...


class MemeTagReplacer(Protocol):
    def __call__(self, config: LLMChatConfig, relative_path: str, tags: str) -> Awaitable[None]: ...


class MemeDeleter(Protocol):
    def __call__(self, relative_path: str) -> Awaitable[MemeDeleteResult]: ...


class MemeTagger(Protocol):
    def __call__(self, config: LLMChatConfig, data_url: str) -> Awaitable[str]: ...


class MemeAdminError(RuntimeError):
    """A bounded administrative failure safe to return through the WebUI API."""

    def __init__(self, message: str, *, code: str, status: int) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True, slots=True)
class MemeUploadOutcome:
    status: str
    item: MemeCatalogItem


class MemeAdminService:
    """Coordinate catalog reads with bounded image and index mutations."""

    def __init__(
        self,
        config: LLMChatConfig,
        *,
        session_factory: SessionFactory = get_session,
        meme_dir: Path = MEME_DIR,
        catalog: MemeCatalog | None = None,
        importer: MemeBytesImporter = import_meme_bytes,
        tag_replacer: MemeTagReplacer = replace_image_tags,
        deleter: MemeDeleter = delete_meme,
        tagger: MemeTagger = generate_image_tags,
    ) -> None:
        self._config = config
        self._catalog = catalog or MemeCatalog(session_factory=session_factory, meme_dir=meme_dir)
        self._importer = importer
        self._tag_replacer = tag_replacer
        self._deleter = deleter
        self._tagger = tagger

    def resolve_file(self, file_name: str) -> Path | None:
        return self._catalog.resolve_file(file_name)

    async def list_memes(
        self,
        *,
        query: str = "",
        status: MemeCatalogFilter = "all",
        sort: MemeCatalogSort = "newest",
        page: int = 1,
        page_size: int = 24,
    ) -> MemeCatalogPage:
        try:
            return await self._catalog.list_memes(
                query=query,
                status=status,
                sort=sort,
                page=page,
                page_size=page_size,
            )
        except MemeCatalogError:
            raise MemeAdminError(
                "Meme catalog could not be loaded",
                code="catalog_unavailable",
                status=500,
            ) from None

    async def get_item(self, file_name: str) -> MemeCatalogItem:
        try:
            return await self._catalog.get_item(file_name)
        except KeyError:
            raise MemeAdminError("Meme entry was not found", code="meme_not_found", status=404) from None

    async def upload(self, data: bytes, *, tags: str = "", auto_tag: bool = True) -> MemeUploadOutcome:
        manual_tags = tags.strip() or None
        if manual_tags is not None and len(manual_tags) > 4000:
            raise MemeAdminError("Tag text is too long", code="invalid_tags", status=400)
        if manual_tags is not None:
            normalized = normalize_generated_image_tags(manual_tags)
            if not normalized:
                raise MemeAdminError("At least one valid tag is required", code="invalid_tags", status=400)
            manual_tags = normalized
        try:
            result = await self._importer(
                self._config,
                data,
                manual_tags=manual_tags,
                auto_tag=auto_tag,
            )
        except asyncio.CancelledError:
            raise
        except MemeImportError as exc:
            raise MemeAdminError(str(exc), code="upload_rejected", status=400) from None
        except Exception as exc:
            _LOGGER.warning(f"meme upload failed: {type(exc).__name__}")
            raise MemeAdminError("Meme upload failed", code="upload_failed", status=500) from None
        file_name = MemeCatalog.managed_file_name(result.relative_path)
        if file_name is None:
            raise MemeAdminError("Stored meme path is invalid", code="storage_inconsistent", status=500)
        return MemeUploadOutcome(status=result.status, item=await self.get_item(file_name))

    async def update_tags(self, file_name: str, tags: str) -> MemeCatalogItem:
        if len(tags) > 4000:
            raise MemeAdminError("Tag text is too long", code="invalid_tags", status=400)
        normalized = normalize_generated_image_tags(tags)
        if not normalized:
            raise MemeAdminError("At least one valid tag is required", code="invalid_tags", status=400)
        item = await self.get_item(file_name)
        try:
            await self._tag_replacer(self._config, item.storage_path, normalized)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _LOGGER.warning(f"meme tag update failed: {type(exc).__name__}")
            raise MemeAdminError("Meme tags could not be updated", code="tag_update_failed", status=500) from None
        return await self.get_item(file_name)

    async def retag(self, file_name: str) -> MemeCatalogItem:
        item = await self.get_item(file_name)
        path = self.resolve_file(file_name)
        if path is None or not path.is_file():
            raise MemeAdminError("Meme image file is missing", code="image_missing", status=409)
        data_url = await asyncio.to_thread(image_file_to_data_url, path)
        if data_url is None:
            raise MemeAdminError("Meme image is invalid or too large", code="image_unavailable", status=400)
        try:
            tags = normalize_generated_image_tags(await self._tagger(self._config, data_url))
            if not tags:
                raise MemeAdminError("Automatic tagging returned no tags", code="tagging_failed", status=502)
            await self._tag_replacer(self._config, item.storage_path, tags)
        except asyncio.CancelledError:
            raise
        except MemeAdminError:
            raise
        except Exception as exc:
            _LOGGER.warning(f"meme retag failed: {type(exc).__name__}")
            raise MemeAdminError("Automatic tagging failed", code="tagging_failed", status=502) from None
        return await self.get_item(file_name)

    async def delete(self, file_name: str) -> MemeDeleteResult:
        item = await self.get_item(file_name)
        try:
            result = await self._deleter(item.storage_path)
        except asyncio.CancelledError:
            raise
        except MemeImportError:
            raise MemeAdminError("Meme deletion failed", code="delete_failed", status=500) from None
        except Exception as exc:
            _LOGGER.warning(f"meme delete failed: {type(exc).__name__}")
            raise MemeAdminError("Meme deletion failed", code="delete_failed", status=500) from None
        if not result.file_deleted and not result.index_deleted:
            raise MemeAdminError("Meme entry was not found", code="meme_not_found", status=404)
        return result
