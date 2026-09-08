"""Durable private image attachments for AgentEvent audit views."""

from __future__ import annotations

import os
import re
from typing import Literal
import asyncio
from pathlib import Path
from secrets import token_hex
from collections.abc import Mapping, Callable, Sequence

from arclet.entari import Image, Session, local_data

from .core.image_source import IMAGE_FETCH_MAX_BYTES, fetch_image_bytes, raw_to_image_data_url

AttachmentKind = Literal["input", "reference", "output"]
MAX_INPUT_ATTACHMENTS = 6
MAX_EVENT_ATTACHMENTS = 8
_ATTACHMENT_REF = re.compile(r"(?P<kind>input|reference|output)_[0-9a-f]{32}\Z")
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


def _attachment_kind(attachment_ref: object) -> str:
    if not isinstance(attachment_ref, str):
        return ""
    matched = _ATTACHMENT_REF.fullmatch(attachment_ref)
    return matched.group("kind") if matched is not None else ""


def is_agent_attachment(attachment_ref: object, mime: object) -> bool:
    """Return whether payload metadata can name one managed audit attachment."""

    return bool(_attachment_kind(attachment_ref) and isinstance(mime, str) and mime.casefold() in _MIME_SUFFIXES)


def is_user_input_attachment(attachment_ref: object, mime: object) -> bool:
    """Return whether payload metadata names one managed user-input attachment."""

    return _attachment_kind(attachment_ref) == "input" and is_agent_attachment(attachment_ref, mime)


def store_agent_attachment(
    data: bytes,
    *,
    kind: AttachmentKind,
    source: str,
    index: int,
    label: str = "",
    description: str = "",
    root: Path | None = None,
) -> dict[str, object]:
    """Persist one sniffed bounded image and return opaque event metadata."""

    if not isinstance(data, bytes) or not data or len(data) > IMAGE_FETCH_MAX_BYTES:
        raise ValueError("invalid attachment image bytes")
    data_url = raw_to_image_data_url(data)
    if data_url is None:
        raise ValueError("unsupported attachment image")
    mime = data_url[5:].partition(";")[0].casefold()
    if mime not in _MIME_SUFFIXES:
        raise ValueError("unsupported attachment MIME")
    attachment_ref = f"{kind}_{token_hex(16)}"
    path = _attachment_path(attachment_ref, mime, root=root)
    _write_attachment(path, data)
    metadata: dict[str, object] = {
        "attachment_ref": attachment_ref,
        "mime": mime,
        "bytes": len(data),
        "source": source[:80],
        "index": max(1, int(index)),
    }
    normalized_label = " ".join(label.split())[:160]
    normalized_description = " ".join(description.split())[:500]
    if normalized_label:
        metadata["label"] = normalized_label
    if normalized_description:
        metadata["description"] = normalized_description
    return metadata


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
    try:
        return store_agent_attachment(
            data,
            kind="input",
            source="quoted" if quoted else "direct",
            index=index,
            root=root,
        )
    except ValueError:
        return None


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
        remove_agent_attachments([result for result in settled if isinstance(result, dict)], root=root)
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


def resolve_agent_attachment(
    attachment_ref: str,
    mime: str,
    *,
    root: Path | None = None,
) -> Path:
    """Resolve one validated opaque audit attachment to a private file."""

    return _attachment_path(attachment_ref.strip(), mime.strip().casefold(), root=root)


def resolve_user_input_attachment(
    attachment_ref: str,
    mime: str,
    *,
    root: Path | None = None,
) -> Path:
    """Resolve one validated opaque user-input attachment to a private file."""

    if _attachment_kind(attachment_ref.strip()) != "input":
        raise ValueError("invalid input attachment reference")
    return resolve_agent_attachment(attachment_ref, mime, root=root)


def event_attachment_metadata(payload: Mapping[str, object]) -> list[Mapping[str, object]]:
    """Return event-authorized attachments from input metadata and tool evidence."""

    attachments: list[Mapping[str, object]] = []
    direct = payload.get("attachments")
    if isinstance(direct, Sequence) and not isinstance(direct, (str, bytes)):
        attachments.extend(item for item in direct if isinstance(item, Mapping))
    evidence = payload.get("evidence")
    if isinstance(evidence, Mapping):
        related = evidence.get("attachments")
        if isinstance(related, Sequence) and not isinstance(related, (str, bytes)):
            attachments.extend(item for item in related if isinstance(item, Mapping))
    return attachments[:MAX_EVENT_ATTACHMENTS]


def remove_agent_attachments(
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
            path = resolve_agent_attachment(attachment_ref, mime, root=root)
        except ValueError:
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError:
            continue


def remove_user_input_attachments(
    attachments: Sequence[Mapping[str, object]],
    *,
    root: Path | None = None,
) -> None:
    """Backward-compatible user-input attachment compensation."""

    remove_agent_attachments(attachments, root=root)
