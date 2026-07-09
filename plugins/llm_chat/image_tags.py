"""Local image tagging and image-selection runtime."""

from __future__ import annotations

import base64
import random
from typing import TypeAlias
import asyncio
from pathlib import Path
from collections import deque
from collections.abc import Callable, Sequence, Awaitable

from arclet.entari.logger import log
from entari_plugin_database import select, get_session

from utils.path import IMAGE_DIR
from utils.llm_chat_core.media import match_image, is_random_request
from utils.llm_chat_core.profile import decode_embedding, encode_embedding, cosine_similarity

from .config import LLMChatConfig
from .models import ImageTag
from .vision import generate_image_tags
from .persona.embedding import embed_text

ProgressReporter: TypeAlias = Callable[[int, int, int], Awaitable[None]]

_LOGGER = log.wrapper("[llm_chat]")
_image_vectors: dict[str, list[float]] = {}


async def pick_image(config: LLMChatConfig, rows: Sequence[ImageTag], context: str, recent: deque[str]) -> str | None:
    """Select a local image via semantic retrieval, falling back to tag IDF."""
    paths = [row.file_path for row in rows]
    if is_random_request(context):
        pool = [path for path in paths if path not in recent] or paths
        return random.choice(pool) if pool else None
    query = await embed_text(config, context)
    if query is not None:
        candidates: list[tuple[str, float]] = []
        for row in rows:
            vector = _image_vectors.get(row.file_path)
            if vector is None:
                vector = decode_embedding(row.embedding_json)
                if vector is None:
                    continue
                _image_vectors[row.file_path] = vector
            score = cosine_similarity(query, vector)
            if score >= config.image_match_min_similarity:
                candidates.append((row.file_path, score))
        candidates.sort(key=lambda item: item[1], reverse=True)
        top = [path for path, _score in candidates[: config.image_top_candidates]]
        pool = [path for path in top if path not in recent] or top
        if pool:
            return random.choice(pool)
    tagged = [(row.file_path, row.tags) for row in rows]
    fallback = [(path, tags) for path, tags in tagged if path not in recent] or tagged
    return match_image(context, fallback)


async def tag_images(
    config: LLMChatConfig,
    limit: int | None = None,
    *,
    retag: bool = False,
    on_progress: ProgressReporter | None = None,
) -> tuple[int, int, int]:
    """Tag local images with vision keywords and return tagged, failed, remaining."""
    counter = {"tagged": 0, "failed": 0}
    remaining = 0
    try:
        async with get_session() as db:
            known = {row.file_path for row in (await db.execute(select(ImageTag))).scalars().all()}
        candidates = [
            path
            for path in sorted(IMAGE_DIR.rglob("*"))
            if path.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")
            and (retag or str(path.relative_to(IMAGE_DIR)) not in known)
        ]
        batch = candidates if limit is None else candidates[: max(0, limit)]
        remaining = max(0, len(candidates) - len(batch))
        total = len(batch)
        if not batch:
            if on_progress is not None:
                await on_progress(0, 0, 0)
            return counter["tagged"], counter["failed"], remaining
        scope = "retagging" if retag else "tagging"
        _LOGGER.info(f"{scope} {total} images ({remaining} remain)")
        if on_progress is not None:
            await on_progress(0, 0, total)
        semaphore = asyncio.Semaphore(max(1, config.tag_concurrency))

        async def _tag_one(path: Path) -> None:
            rel_path = str(path.relative_to(IMAGE_DIR))
            async with semaphore:
                try:
                    data = base64.b64encode(path.read_bytes()).decode()
                    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
                    tags = await generate_image_tags(config, f"data:{mime};base64,{data}")
                    if not tags:
                        counter["failed"] += 1
                        return
                    vector = await embed_text(config, tags)
                    embedding_json = encode_embedding(vector) if vector is not None else ""
                    async with get_session() as db:
                        existing = (
                            await db.execute(select(ImageTag).where(ImageTag.file_path == rel_path))
                        ).scalar_one_or_none()
                        if existing is None:
                            db.add(ImageTag(file_path=rel_path, tags=tags, embedding_json=embedding_json))
                        else:
                            existing.tags = tags
                            if embedding_json:
                                existing.embedding_json = embedding_json
                        await db.commit()
                    _image_vectors.pop(rel_path, None)
                    counter["tagged"] += 1
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    counter["failed"] += 1
                    _LOGGER.warning(f"tagging failed for {path.name}: {exc!r}")
            done = counter["tagged"] + counter["failed"]
            if on_progress is not None and done % 50 == 0 and done < total:
                await on_progress(counter["tagged"], counter["failed"], total)

        await asyncio.gather(*(_tag_one(path) for path in batch))
        if on_progress is not None:
            await on_progress(counter["tagged"], counter["failed"], total)
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        _LOGGER.warning(f"image tagging pass aborted: {exc!r}")
    return counter["tagged"], counter["failed"], remaining
