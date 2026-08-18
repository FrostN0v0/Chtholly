"""Read model for managed meme files and searchable index rows."""

from __future__ import annotations

from os import stat_result
from typing import Any, Literal, TypeAlias
import asyncio
from pathlib import Path, PurePosixPath
from datetime import datetime, timezone
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from collections.abc import Callable

from arclet.entari.logger import log
from entari_plugin_database import select, get_session

from utils.path import MEME_DIR

from .models import ImageTag
from .core.media import normalize_image_tags

MemeCatalogStatus = Literal["indexed", "unindexed", "missing"]
MemeCatalogFilter = Literal["all", "indexed", "unindexed", "missing"]
MemeCatalogSort = Literal["newest", "oldest", "name"]
SessionFactory: TypeAlias = Callable[[], AbstractAsyncContextManager[Any]]

_SUPPORTED_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif"})
_LOGGER = log.wrapper("[llm_chat]")


class MemeCatalogError(RuntimeError):
    """A catalog read failure safe to expose through the administration boundary."""


@dataclass(frozen=True, slots=True)
class MemeCatalogItem:
    entry_id: int | None
    file_name: str
    relative_path: str
    storage_path: str
    tags: str
    status: MemeCatalogStatus
    size_bytes: int | None
    modified_at: str | None
    modified_timestamp: float
    version: int | None
    embedding_ready: bool

    def to_dict(self) -> dict[str, object]:
        normalized_tags = normalize_image_tags(self.tags, limit=100)
        return {
            "id": self.entry_id,
            "file_name": self.file_name,
            "relative_path": self.relative_path,
            "tags": self.tags,
            "tag_count": len(normalized_tags.split("，")) if normalized_tags else 0,
            "status": self.status,
            "size_bytes": self.size_bytes,
            "modified_at": self.modified_at,
            "version": self.version,
            "embedding_ready": self.embedding_ready,
            "has_file": self.status != "missing",
            "has_index": self.entry_id is not None,
        }


@dataclass(frozen=True, slots=True)
class MemeCatalogPage:
    items: tuple[MemeCatalogItem, ...]
    total: int
    page: int
    page_size: int
    pages: int
    stats: dict[str, int]


class MemeCatalog:
    """Join managed files with ImageTag rows without exposing absolute paths."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory = get_session,
        meme_dir: Path = MEME_DIR,
    ) -> None:
        self._session_factory = session_factory
        self._meme_dir = meme_dir

    @staticmethod
    def managed_file_name(relative_path: str) -> str | None:
        normalized = PurePosixPath(relative_path.replace("\\", "/"))
        if (
            len(normalized.parts) != 2
            or normalized.parts[0].casefold() != "memes"
            or normalized.suffix.casefold() not in _SUPPORTED_SUFFIXES
        ):
            return None
        return normalized.name

    def resolve_file(self, file_name: str) -> Path | None:
        normalized = PurePosixPath(file_name)
        if (
            len(normalized.parts) != 1
            or normalized.name != file_name
            or normalized.suffix.casefold() not in _SUPPORTED_SUFFIXES
        ):
            return None
        try:
            candidate = (self._meme_dir / file_name).resolve()
            if candidate.parent != self._meme_dir.resolve():
                return None
        except (OSError, ValueError):
            return None
        return candidate

    async def _load_rows(self) -> list[ImageTag]:
        async with self._session_factory() as db:
            return list((await db.execute(select(ImageTag))).scalars().all())

    def _scan_files(self) -> dict[str, stat_result]:
        if not self._meme_dir.exists():
            return {}
        root = self._meme_dir.resolve()
        files: dict[str, stat_result] = {}
        for path in self._meme_dir.iterdir():
            try:
                resolved = path.resolve()
                if (
                    path.is_symlink()
                    or resolved.parent != root
                    or not resolved.is_file()
                    or resolved.suffix.casefold() not in _SUPPORTED_SUFFIXES
                ):
                    continue
                files[path.name] = resolved.stat()
            except OSError:
                continue
        return files

    async def _snapshot(self) -> tuple[list[MemeCatalogItem], dict[str, int]]:
        try:
            rows, files = await asyncio.gather(self._load_rows(), asyncio.to_thread(self._scan_files))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _LOGGER.warning(f"meme catalog load failed: {type(exc).__name__}")
            raise MemeCatalogError("Meme catalog could not be loaded") from None

        rows_by_name: dict[str, ImageTag] = {}
        for row in rows:
            file_name = self.managed_file_name(row.file_path)
            if file_name is None:
                continue
            existing = rows_by_name.get(file_name)
            if existing is None or row.id > existing.id:
                rows_by_name[file_name] = row

        items: list[MemeCatalogItem] = []
        for file_name in sorted(set(files) | set(rows_by_name)):
            row = rows_by_name.get(file_name)
            file_entry = files.get(file_name)
            if file_entry is None:
                status: MemeCatalogStatus = "missing"
                size_bytes = None
                modified_at = None
                modified_timestamp = 0.0
                version = None
            else:
                stat = file_entry
                status = "indexed" if row is not None and row.tags.strip() else "unindexed"
                size_bytes = int(stat.st_size)
                modified_timestamp = float(stat.st_mtime)
                modified_at = datetime.fromtimestamp(modified_timestamp, timezone.utc).isoformat()
                version = int(stat.st_mtime_ns)
            storage_path = row.file_path if row is not None else str(Path("memes") / file_name)
            items.append(
                MemeCatalogItem(
                    entry_id=row.id if row is not None else None,
                    file_name=file_name,
                    relative_path=f"memes/{file_name}",
                    storage_path=storage_path,
                    tags=row.tags if row is not None else "",
                    status=status,
                    size_bytes=size_bytes,
                    modified_at=modified_at,
                    modified_timestamp=modified_timestamp,
                    version=version,
                    embedding_ready=bool(row is not None and row.embedding_json.strip()),
                )
            )

        stats = {
            "stored": sum(item.status != "missing" for item in items),
            "indexed": sum(item.status == "indexed" for item in items),
            "unindexed": sum(item.status == "unindexed" for item in items),
            "missing": sum(item.status == "missing" for item in items),
        }
        return items, stats

    async def list_memes(
        self,
        *,
        query: str = "",
        status: MemeCatalogFilter = "all",
        sort: MemeCatalogSort = "newest",
        page: int = 1,
        page_size: int = 24,
    ) -> MemeCatalogPage:
        items, stats = await self._snapshot()
        normalized_query = query.strip().casefold()[:200]
        if normalized_query:
            items = [
                item
                for item in items
                if normalized_query in item.file_name.casefold()
                or normalized_query in item.relative_path.casefold()
                or normalized_query in item.tags.casefold()
            ]
        if status != "all":
            items = [item for item in items if item.status == status]

        if sort == "name":
            items.sort(key=lambda item: item.file_name.casefold())
        else:
            items.sort(
                key=lambda item: (item.modified_timestamp, item.entry_id or 0, item.file_name.casefold()),
                reverse=sort == "newest",
            )

        page_size = min(100, max(1, page_size))
        total = len(items)
        pages = max(1, (total + page_size - 1) // page_size)
        page = min(max(1, page), pages)
        start = (page - 1) * page_size
        return MemeCatalogPage(
            items=tuple(items[start : start + page_size]),
            total=total,
            page=page,
            page_size=page_size,
            pages=pages,
            stats=stats,
        )

    async def get_item(self, file_name: str) -> MemeCatalogItem:
        items, _stats = await self._snapshot()
        for item in items:
            if item.file_name == file_name:
                return item
        raise KeyError(file_name)
