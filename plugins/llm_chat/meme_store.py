"""Atomic storage for collected reaction images."""

from __future__ import annotations

import os
from uuid import uuid4
from typing import Literal
import asyncio
import hashlib
from pathlib import Path
from dataclasses import dataclass

from arclet.entari import Image, Session
from arclet.entari.logger import log

from utils.path import MEME_DIR, IMAGE_DIR

from .config import LLMChatConfig
from .vision import generate_image_tags
from .image_tags import get_image_tag, upsert_image_tag
from .core.errors import summarize_exception
from .core.image_source import fetch_image_bytes, raw_to_image_data_url

MemeImportStatus = Literal["created", "duplicate", "tagged_existing"]


@dataclass(frozen=True, slots=True)
class MemeImportResult:
    status: MemeImportStatus
    relative_path: str
    tags: str


class MemeImportError(RuntimeError):
    """A meme import failure whose message is safe to show to users."""


_LOGGER = log.wrapper("[llm_chat]")
_SUPPORTED_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif"})
_MIME_SUFFIXES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
_import_lock = asyncio.Lock()
_indexed_root: Path | None = None
_digest_paths: dict[str, Path] = {}


def _redacted_exception_summary(exc: BaseException) -> str:
    summary = summarize_exception(exc)
    replacements: set[str] = set()
    for path in (IMAGE_DIR, MEME_DIR):
        replacements.add(str(path))
        try:
            replacements.add(str(path.resolve()))
        except OSError:
            pass
    for value in sorted(replacements, key=len, reverse=True):
        if value:
            summary = summary.replace(value, "[RESOURCE_DIR]")
    return summary


def _safe_import_error(message: str, exc: BaseException) -> MemeImportError:
    _LOGGER.warning(f"meme import failed: {_redacted_exception_summary(exc)}")
    return MemeImportError(message)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _initialize_digest_index() -> None:
    global _indexed_root, _digest_paths

    root = MEME_DIR.resolve()
    if _indexed_root == root:
        return

    MEME_DIR.mkdir(parents=True, exist_ok=True)
    for temp_path in MEME_DIR.glob(".meme-*.tmp"):
        temp_path.unlink(missing_ok=True)

    digest_paths: dict[str, Path] = {}
    for path in sorted(MEME_DIR.iterdir()):
        if not path.is_file() or path.suffix.lower() not in _SUPPORTED_SUFFIXES:
            continue
        digest_paths.setdefault(_hash_file(path), path)

    _digest_paths = digest_paths
    _indexed_root = root


def _relative_path(path: Path) -> str:
    return str(path.relative_to(IMAGE_DIR))


def _next_numeric_stem() -> int:
    numeric_stems = [int(path.stem) for path in MEME_DIR.iterdir() if path.is_file() and path.stem.isdigit()]
    return max(numeric_stems, default=0) + 1


def _write_temp_file(path: Path, data: bytes) -> None:
    with path.open("xb") as stream:
        written = stream.write(data)
        if written != len(data):
            raise OSError("short meme temp write")


def _cleanup_file(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        _LOGGER.warning(f"meme cleanup failed: {_redacted_exception_summary(exc)}")


async def _generate_tags(config: LLMChatConfig, data_url: str) -> str:
    try:
        tags = await generate_image_tags(config, data_url)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise _safe_import_error("Automatic image tagging failed", exc) from None
    if not tags:
        raise MemeImportError("Image tagging returned no tags")
    return tags


async def _settle_upsert_after_cancellation(task: asyncio.Task[None]) -> BaseException | None:
    while True:
        try:
            await asyncio.shield(task)
            return None
        except asyncio.CancelledError:
            if not task.done():
                continue
            try:
                task.result()
            except BaseException as task_exc:
                return task_exc
            return None
        except BaseException as exc:
            return exc


async def _commit_new_tag(
    config: LLMChatConfig,
    relative_path: str,
    tags: str,
    digest: str,
    final_path: Path,
) -> None:
    task = asyncio.create_task(upsert_image_tag(config, relative_path, tags))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        upsert_error = await _settle_upsert_after_cancellation(task)
        if upsert_error is None:
            _digest_paths[digest] = final_path
        else:
            _cleanup_file(final_path)
            _LOGGER.warning(f"meme tag commit failed: {_redacted_exception_summary(upsert_error)}")
        raise
    except Exception as exc:
        _cleanup_file(final_path)
        raise _safe_import_error("Image tag persistence failed", exc) from None
    else:
        _digest_paths[digest] = final_path


async def import_meme_image(
    config: LLMChatConfig,
    session: Session,
    image: Image,
) -> MemeImportResult:
    """Download, deduplicate, tag, and atomically publish one reaction image."""
    data = await fetch_image_bytes(session, image.src)
    if data is None:
        raise MemeImportError("Image data is unavailable, invalid, or too large")

    data_url = raw_to_image_data_url(data)
    if data_url is None:
        raise MemeImportError("Image format could not be recognized")
    mime = data_url[5:].partition(";")[0]
    suffix = _MIME_SUFFIXES.get(mime)
    if suffix is None:
        raise MemeImportError("Unsupported image format; only JPEG, PNG, WebP, and GIF are accepted")

    digest = hashlib.sha256(data).hexdigest()
    async with _import_lock:
        try:
            _initialize_digest_index()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _safe_import_error("Meme storage failed", exc) from None

        existing_path = _digest_paths.get(digest)
        if existing_path is not None and not existing_path.exists():
            _digest_paths.pop(digest, None)
            existing_path = None

        if existing_path is not None:
            try:
                relative_path = _relative_path(existing_path)
            except Exception as exc:
                raise _safe_import_error("Meme storage failed", exc) from None
            try:
                existing_tag = await get_image_tag(relative_path)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise _safe_import_error("Image tag lookup failed", exc) from None
            if existing_tag is not None and existing_tag.tags.strip():
                return MemeImportResult("duplicate", relative_path, existing_tag.tags)
            tags = await _generate_tags(config, data_url)
            try:
                await upsert_image_tag(config, relative_path, tags)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise _safe_import_error("Image tag persistence failed", exc) from None
            return MemeImportResult("tagged_existing", relative_path, tags)

        tags = await _generate_tags(config, data_url)
        temp_path: Path | None = MEME_DIR / f".meme-{uuid4().hex}.tmp"
        final_path: Path | None = None
        try:
            _write_temp_file(temp_path, data)

            stem = _next_numeric_stem()
            while True:
                candidate = MEME_DIR / f"{stem}{suffix}"
                try:
                    os.link(temp_path, candidate)
                except FileExistsError:
                    stem += 1
                    continue
                final_path = candidate
                break
            temp_path.unlink()
            temp_path = None
        except asyncio.CancelledError:
            _cleanup_file(temp_path)
            _cleanup_file(final_path)
            raise
        except Exception as exc:
            _cleanup_file(temp_path)
            _cleanup_file(final_path)
            raise _safe_import_error("Meme storage failed", exc) from None

        assert final_path is not None
        try:
            relative_path = _relative_path(final_path)
        except Exception as exc:
            _cleanup_file(final_path)
            raise _safe_import_error("Meme storage failed", exc) from None
        await _commit_new_tag(config, relative_path, tags, digest, final_path)
        return MemeImportResult("created", relative_path, tags)
