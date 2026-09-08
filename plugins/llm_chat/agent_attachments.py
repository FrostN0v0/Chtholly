"""Durable, private image attachments for user-input AgentEvents."""

from __future__ import annotations

import os
import re
import asyncio
from pathlib import Path
from secrets import token_hex
from collections.abc import Mapping, Callable, Sequence

from arclet.entari import Image, Session, local_data

from .core.image_source import fetch_image_bytes, raw_to_image_data_url

MAX_INPUT_ATTACHMENTS = 6
_ATTACHMENT_REF = re.compile(r"input_[0-9a-f]{32}\Z")
_MIME_SUFFIXES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def _attachment_root(root: Path | None = None) -> Path:
    target = root or (local_data.get_data_dir("llm_chat") / "agent_attachments")
    target.mkdir(parents=True, exist_ok=True)
    return target.resolve()


def _attachment_path(attachment_ref: str, mime: str, *, root: Path | None = None) -> Path:
    if not _ATTACHMENT_REF.fullmatch(attachment_ref):
        raise ValueError("invalid attachment reference")
    suffix = _MIME_SUFFIXES.get(mime)
    if suffix is None:
        raise ValueError("unsupported attachment MIME")
    base = _attachment_root(root)
    target = (base / f"{attachment_ref}{suffix}").resolve()
    if target.parent != base:
        raise ValueError("attachment path escaped its root")
    return target


def _write_attachment(path: Path, data: bytes) -> None:
    temp = path.with_name(f".{path.name}.{token_hex(8)}.tmp")
    try:
        with temp.open("xb") as stream:
            written = stream.write(data)
            if written != len(data):
                raise OSError("short attachment write")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def is_user_input_attachment(attachment_ref: object, mime: object) -> bool:
    """Return whether payload metadata can name one managed input attachment."""

    return (
        isinstance(attachment_ref, str)
        and _ATTACHMENT_REF.fullmatch(attachment_ref) is not None
        and isinstance(mime, str)
        and mime.casefold() in _MIME_SUFFIXES
    )


async def _capture_one(
    session: Session,
    image: Image,
    *,
    quoted: bool,
    index: int,
    root: Path | None,
) -> dict[str, object] | None:
    data = await fetch_image_bytes(session, image.src)
    if data is None:
        return None
    data_url = raw_to_image_data_url(data)
    if data_url is None:
        return None
    mime = data_url[5:].partition(";")[0]
    if mime not in _MIME_SUFFIXES:
        return None
    attachment_ref = f"input_{token_hex(16)}"
    path = _attachment_path(attachment_ref, mime, root=root)
    _write_attachment(path, data)
    return {
        "attachment_ref": attachment_ref,
        "mime": mime,
        "bytes": len(data),
        "source": "quoted" if quoted else "direct",
        "index": index,
    }


async def capture_user_input_images(
    session: Session,
    candidates: Sequence[tuple[Image, bool]],
    *,
    maximum: int = MAX_INPUT_ATTACHMENTS,
    warn: Callable[[str], object] | None = None,
    root: Path | None = None,
) -> list[dict[str, object]]:
    """Copy bounded current-message images into private AgentEvent storage."""

    selected = list(candidates[: max(0, min(MAX_INPUT_ATTACHMENTS, maximum))])
    if not selected:
        return []
    tasks = [
        asyncio.create_task(_capture_one(session, image, quoted=quoted, index=index, root=root))
        for index, (image, quoted) in enumerate(selected, start=1)
    ]
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        settled = await asyncio.gather(*tasks, return_exceptions=True)
        remove_user_input_attachments([result for result in settled if isinstance(result, dict)], root=root)
        raise
    captured: list[dict[str, object]] = []
    for result in results:
        if isinstance(result, BaseException):
            if warn is not None:
                warn(f"user image attachment failed: {type(result).__name__}")
            continue
        if result is not None:
            captured.append(result)
    return captured


def resolve_user_input_attachment(
    attachment_ref: str,
    mime: str,
    *,
    root: Path | None = None,
) -> Path:
    """Resolve one validated opaque attachment reference to a private file."""

    return _attachment_path(attachment_ref.strip(), mime.strip().casefold(), root=root)


def remove_user_input_attachments(
    attachments: Sequence[Mapping[str, object]],
    *,
    root: Path | None = None,
) -> None:
    """Best-effort compensation for attachments that never reached an AgentEvent."""

    for item in attachments:
        attachment_ref = item.get("attachment_ref")
        mime = item.get("mime")
        if not isinstance(attachment_ref, str) or not isinstance(mime, str):
            continue
        try:
            path = _attachment_path(attachment_ref, mime, root=root)
        except ValueError:
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError:
            continue
