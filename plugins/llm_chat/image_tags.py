"""Local image tagging and image-selection runtime."""

from __future__ import annotations

import random
from typing import TypeAlias
import asyncio
from pathlib import Path
from collections import deque
from collections.abc import Callable, Sequence, Awaitable

from arclet.entari.logger import log
from entari_plugin_database import select, get_session

from utils.path import IMAGE_DIR

from .config import LLMChatConfig
from .models import ImageTag
from .vision import generate_image_tags
from .core.media import (
    match_image,
    is_random_request,
    rank_images_by_exact_tags,
    is_allowed_image_resource_path,
)
from .core.errors import summarize_exception
from .core.profile import decode_embedding, encode_embedding, cosine_similarity
from .core.image_source import image_file_to_data_url
from .persona.embedding import embed_text
from .core.image_tag_metadata import (
    image_tag_format,
    image_tag_search_text,
    image_tag_avoids_context,
    image_tag_embedding_text,
    image_tag_has_visible_text,
    normalize_generated_image_tags,
)

ProgressReporter: TypeAlias = Callable[[int, int, int], Awaitable[None]]

_LOGGER = log.wrapper("[llm_chat]")
_image_vectors: dict[str, list[float]] = {}
_image_tag_lock = asyncio.Lock()
_SEMANTIC_NEAR_WINDOW = 0.04
_TEXT_IMAGE_MIN_SIMILARITY = 0.40


async def pick_image(config: LLMChatConfig, rows: Sequence[ImageTag], context: str, recent: deque[str]) -> str | None:
    """Select a local image via semantic retrieval, falling back to tag IDF."""
    eligible_rows = [row for row in rows if not image_tag_avoids_context(row.tags, context)]
    exact_ranked = rank_images_by_exact_tags(
        context,
        [(row.file_path, image_tag_search_text(row.tags)) for row in eligible_rows],
    )
    candidate_rows = eligible_rows
    if exact_ranked:
        best_exact_score = exact_ranked[0][1]
        exact_paths = {path for path, score in exact_ranked if score >= best_exact_score - 1e-9}
        candidate_rows = [row for row in eligible_rows if row.file_path in exact_paths]
    fresh_rows = [row for row in candidate_rows if row.file_path not in recent]
    search_rows = fresh_rows or candidate_rows
    if not search_rows:
        return None
    paths = [row.file_path for row in search_rows]
    if is_random_request(context):
        return random.choice(paths)
    query = await embed_text(config, context)
    if query is not None:
        candidates: list[tuple[str, float]] = []
        for row in search_rows:
            vector = _image_vectors.get(row.file_path)
            if vector is None:
                vector = decode_embedding(row.embedding_json)
                if vector is None:
                    continue
                _image_vectors[row.file_path] = vector
            score = cosine_similarity(query, vector)
            threshold = config.image_match_min_similarity
            if image_tag_has_visible_text(row.tags):
                threshold = max(threshold, _TEXT_IMAGE_MIN_SIMILARITY)
            if score >= threshold:
                candidates.append((row.file_path, score))
        candidates.sort(key=lambda item: item[1], reverse=True)
        if candidates:
            best = candidates[0][1]
            top = [
                path
                for path, score in candidates[: config.image_top_candidates]
                if score >= best - _SEMANTIC_NEAR_WINDOW
            ]
            if top:
                return random.choice(top)
    tagged = [(row.file_path, image_tag_search_text(row.tags)) for row in search_rows]
    return match_image(context, tagged)


def _canonical_relative_path(relative_path: str) -> str:
    return relative_path.replace("\\", "/")


def _path_variants(relative_path: str) -> tuple[str, ...]:
    canonical = _canonical_relative_path(relative_path)
    native = canonical.replace("/", "\\")
    return (canonical,) if native == canonical else (canonical, native)


async def get_image_tag(relative_path: str) -> ImageTag | None:
    """Load one persisted image tag row by normalized relative resource path."""
    canonical = _canonical_relative_path(relative_path)
    async with get_session() as db:
        rows = list(
            (await db.execute(select(ImageTag).where(ImageTag.file_path.in_(_path_variants(canonical))))).scalars()
        )
    return next((row for row in rows if row.file_path == canonical), rows[0] if rows else None)


async def _write_image_tag(config: LLMChatConfig, relative_path: str, tags: str) -> None:
    normalized = normalize_generated_image_tags(tags)
    if not normalized:
        raise ValueError("Image tag metadata is invalid")
    vector = await embed_text(config, image_tag_embedding_text(normalized))
    embedding_json = encode_embedding(vector) if vector is not None else ""
    canonical = _canonical_relative_path(relative_path)
    variants = _path_variants(canonical)
    async with _image_tag_lock:
        async with get_session() as db:
            rows = list((await db.execute(select(ImageTag).where(ImageTag.file_path.in_(variants)))).scalars())
            existing = next((row for row in rows if row.file_path == canonical), rows[0] if rows else None)
            if existing is None:
                db.add(ImageTag(file_path=canonical, tags=normalized, embedding_json=embedding_json))
            else:
                existing.file_path = canonical
                existing.tags = normalized
                existing.embedding_json = embedding_json
                for duplicate in rows:
                    if duplicate is not existing:
                        await db.delete(duplicate)
            await db.commit()
        for variant in variants:
            _image_vectors.pop(variant, None)


async def upsert_image_tag(config: LLMChatConfig, relative_path: str, tags: str) -> None:
    """Persist canonical metadata and collapse separator-only duplicate rows."""
    await _write_image_tag(config, relative_path, tags)


async def replace_image_tags(config: LLMChatConfig, relative_path: str, tags: str) -> None:
    """Replace one tag row with canonical structured metadata."""
    await _write_image_tag(config, relative_path, tags)


async def delete_image_tag(relative_path: str) -> bool:
    """Delete persisted path variants and their process-local vector cache."""
    variants = _path_variants(relative_path)
    async with _image_tag_lock:
        async with get_session() as db:
            rows = list((await db.execute(select(ImageTag).where(ImageTag.file_path.in_(variants)))).scalars())
            if not rows:
                return False
            for row in rows:
                await db.delete(row)
            await db.commit()
        for variant in variants:
            _image_vectors.pop(variant, None)
    return True


async def tag_images(
    config: LLMChatConfig,
    limit: int | None = None,
    *,
    retag: bool = False,
    legacy_only: bool = False,
    on_progress: ProgressReporter | None = None,
) -> tuple[int, int, int]:
    """Tag local images with vision keywords and return tagged, failed, remaining."""
    counter = {"tagged": 0, "failed": 0}
    remaining = 0
    try:
        async with get_session() as db:
            known: dict[str, str] = {}
            for row in (await db.execute(select(ImageTag))).scalars().all():
                key = _canonical_relative_path(row.file_path)
                if key not in known or image_tag_format(row.tags) == "legacy":
                    known[key] = row.tags
        candidates: list[Path] = []
        for path in sorted(IMAGE_DIR.rglob("*")):
            if path.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
                continue
            relative_path = path.relative_to(IMAGE_DIR).as_posix()
            if not is_allowed_image_resource_path(relative_path):
                continue
            if legacy_only:
                if image_tag_format(known.get(relative_path, "")) != "legacy":
                    continue
            elif not retag and relative_path in known:
                continue
            candidates.append(path)
        batch = candidates if limit is None else candidates[: max(0, limit)]
        remaining = max(0, len(candidates) - len(batch))
        total = len(batch)
        if not batch:
            if on_progress is not None:
                await on_progress(0, 0, 0)
            return counter["tagged"], counter["failed"], remaining
        scope = "retagging" if retag or legacy_only else "tagging"
        _LOGGER.info(f"{scope} {total} images ({remaining} remain)")
        if on_progress is not None:
            await on_progress(0, 0, total)
        semaphore = asyncio.Semaphore(max(1, config.tag_concurrency))

        async def _tag_one(path: Path) -> None:
            rel_path = path.relative_to(IMAGE_DIR).as_posix()
            async with semaphore:
                try:
                    data_url = image_file_to_data_url(path)
                    if data_url is None:
                        counter["failed"] += 1
                        _LOGGER.warning(f"tagging skipped unreadable image {path.name}")
                        return
                    tags = await generate_image_tags(config, data_url)
                    if not tags:
                        counter["failed"] += 1
                        return
                    await upsert_image_tag(config, rel_path, tags)
                    counter["tagged"] += 1
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    counter["failed"] += 1
                    _LOGGER.warning(f"tagging failed for {path.name}: {summarize_exception(exc)}")
            done = counter["tagged"] + counter["failed"]
            if on_progress is not None and done % 50 == 0 and done < total:
                await on_progress(counter["tagged"], counter["failed"], total)

        await asyncio.gather(*(_tag_one(path) for path in batch))
        if on_progress is not None:
            await on_progress(counter["tagged"], counter["failed"], total)
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        _LOGGER.warning(f"image tagging pass aborted: {summarize_exception(exc)}")
    return counter["tagged"], counter["failed"], remaining
