"""Registered image catalog access shared by image tools."""

from __future__ import annotations

from typing import Any
from pathlib import Path
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from collections.abc import Callable, Sequence

from entari_plugin_database import select

from ..models import ImageTag

SessionFactory = Callable[[], AbstractAsyncContextManager[Any]]


def normalize_image_reference(value: str) -> str:
    """Normalize one model-provided registered image reference."""

    return value.strip().strip("`'\"").replace("\\", "/").casefold()


def find_image_row(rows: Sequence[ImageTag], relative_path: str) -> ImageTag | None:
    """Find one catalog row by exact normalized relative path."""

    expected = normalize_image_reference(relative_path)
    return next(
        (row for row in rows if normalize_image_reference(row.file_path) == expected),
        None,
    )


def find_explicit_image_row(rows: Sequence[ImageTag], context: str) -> ImageTag | None:
    """Find the longest registered path explicitly present in tool context."""

    normalized_context = normalize_image_reference(context)
    candidates = sorted(rows, key=lambda row: len(row.file_path), reverse=True)
    return next(
        (
            row
            for row in candidates
            if (reference := normalize_image_reference(row.file_path)) and reference in normalized_context
        ),
        None,
    )


@dataclass
class ImageCatalog:
    """Database-backed registered image catalog with a constrained resource root."""

    image_dir: Path
    session_factory: SessionFactory

    def resolve(self, relative_path: str) -> Path | None:
        """Resolve a registered relative path without escaping the image root."""

        try:
            root = self.image_dir.resolve()
            full = (self.image_dir / relative_path).resolve()
            full.relative_to(root)
        except (OSError, ValueError):
            return None
        return full

    async def load_rows(self) -> list[ImageTag]:
        """Load newest-first rows whose files still exist under the image root."""

        async with self.session_factory() as db:
            rows = list((await db.execute(select(ImageTag))).scalars().all())
        rows.sort(key=lambda row: int(getattr(row, "id", 0) or 0), reverse=True)
        return [row for row in rows if (path := self.resolve(row.file_path)) is not None and path.is_file()]
