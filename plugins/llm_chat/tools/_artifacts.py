"""Shared context, authorization, serialization, and delivery helpers for artifact tools."""

from __future__ import annotations

import re
import json
import asyncio
from dataclasses import dataclass
from collections.abc import Mapping, Callable

from arclet.entari import File, Session, MessageChain

from utils.web_artifacts_core import Artifact, ArtifactOwner, ArtifactFileInfo

from ._delivery import send_with_delivery
from ._rendering import HistoryAppender
from ..agent_context import current_agent_access
from ..core.delivery import (
    DeliveryError,
    reserve_text_message,
    reserve_media_message,
    normalize_delivery_text,
    current_llm_chat_delivery,
)
from ..core.tool_trace import record_tool_evidence
from ..artifacts_runtime import ArtifactLinks, WebArtifactService
from ..core.artifact_access import ArtifactAction, ArtifactAccessError, require_artifact_request

MAX_SOURCE_ZIP_BYTES = 10 * 1024 * 1024

WarningSink = Callable[[str], object]


class ArtifactStoreError(DeliveryError):
    """User-facing wrapper for a managed artifact store failure."""


@dataclass(slots=True)
class ArtifactToolContext:
    """Dependencies shared by all five managed artifact tools."""

    service: WebArtifactService
    append_history: HistoryAppender
    warn: WarningSink
    capture_width: int = 900


@dataclass(frozen=True, slots=True)
class AuthorizedArtifactAccess:
    """Current owner and management privileges derived from AgentAccessContext."""

    owner: ArtifactOwner
    is_operator: bool
    turn_key: str
    raw_user_text: str


def require_authorized_access(action: ArtifactAction) -> AuthorizedArtifactAccess:
    """Bind every tool operation to an active generation and its original speaker."""

    access = current_agent_access()
    if access is None or current_llm_chat_delivery() is None:
        raise DeliveryError("web artifact tools must run inside an active llm_chat generation")
    try:
        require_artifact_request(access.raw_user_text, action)
    except ArtifactAccessError as exc:
        raise DeliveryError(f"artifact operation not allowed: {exc}") from None
    if access.scope_id <= 0 or access.turn_id <= 0 or not access.user_id.strip():
        raise DeliveryError("valid current artifact owner and turn are required")
    return AuthorizedArtifactAccess(
        ArtifactOwner(access.scope_id, access.user_id),
        access.is_operator is True,
        str(access.turn_id),
        access.raw_user_text,
    )


def artifact_metadata(artifact: Artifact, *, include_hash: bool = True) -> dict[str, object]:
    """Keep project metadata without source, capability tokens or owner identifiers."""

    result: dict[str, object] = {
        "artifact_ref": artifact.artifact_ref,
        "project_ref": artifact.project_ref,
        "version": artifact.version,
        "title": artifact.title,
        "entry": artifact.entry,
        "created_at": artifact.created_at,
        "expires_at": artifact.expires_at,
        "file_count": len(artifact.files),
        "source_bytes": artifact.source_bytes,
        "zip_bytes": artifact.zip_bytes,
    }
    if include_hash:
        result["source_sha256"] = artifact.source_sha256
    return result


def artifact_evidence(
    artifact: Artifact,
    *,
    artifact_effect: str,
    **extra: object,
) -> dict[str, object]:
    """Return the audit-safe projection required by Main's event layer."""

    metadata = artifact_metadata(artifact, include_hash=False)
    payload: dict[str, object] = {
        "artifact_effect": artifact_effect,
        "artifact": metadata,
    }
    for key, value in extra.items():
        # Keep evidence flat and bounded; callers pass only scalar status and
        # mode fields, never file content or capability links.
        if key in {"token", "capture_token", "path", "files", "content", "url", "preview_url", "download_url"}:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            payload[key] = value
    return payload


def record_artifact_evidence(
    artifact: Artifact,
    *,
    artifact_effect: str,
    **extra: object,
) -> None:
    """Attach one safe artifact effect to the active tool trace."""

    record_tool_evidence(artifact_evidence(artifact, artifact_effect=artifact_effect, **extra))


def links_payload(service: WebArtifactService, artifact: Artifact) -> ArtifactLinks:
    try:
        return service.links_for(artifact)
    except Exception:
        raise DeliveryError("web artifact public links are unavailable") from None


def _safe_title(value: object) -> str:
    title = " ".join(value.split()) if isinstance(value, str) else "artifact"
    title = re.sub(r"[^\w .-]+", "", title, flags=re.UNICODE).strip(" .")
    title = title[:60] or "artifact"
    return f"{title}.zip"


def build_source_file(artifact: Artifact, data: bytes) -> File:
    """Build an inline Satori File; never hand an adapter a local path/URI."""
    if not isinstance(data, bytes) or not data or len(data) > MAX_SOURCE_ZIP_BYTES:
        raise DeliveryError("source archive is empty or exceeds the delivery size limit")
    try:
        return File.of(raw=data, mime="application/zip", title=_safe_title(artifact.title))
    except (TypeError, ValueError):
        raise DeliveryError("source archive could not be prepared for delivery") from None


def _upload_failure_is_safe_for_link_fallback(exc: BaseException) -> bool:
    """Recognize only adapter capability failures, never unknown transport errors."""

    if isinstance(exc, NotImplementedError):
        return True
    name = type(exc).__name__.casefold()
    text = " ".join(str(exc).split()).casefold()
    if "unsupported" in name or "notimplemented" in name:
        return True
    markers = (
        "unsupported",
        "not supported",
        "unsupported file",
        "file upload is not available",
        "upload is unsupported",
        "cannot serialize file",
        "file messages are unavailable",
    )
    return any(marker in text for marker in markers)


async def deliver_source_archive(
    session: Session,
    runtime: ArtifactToolContext,
    artifact: Artifact,
    data: bytes,
    links: ArtifactLinks,
) -> dict[str, object]:
    """Deliver ZIP bytes, with a narrowly-scoped confirmed link fallback."""

    payload = build_source_file(artifact, data)
    state = current_llm_chat_delivery()
    if state is not None:
        state = reserve_media_message()
    try:
        await send_with_delivery(session, MessageChain([payload]), state, media=True)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if not _upload_failure_is_safe_for_link_fallback(exc):
            raise DeliveryError("source archive delivery failed without a confirmed fallback") from None
        # File upload capability failures are the only unconfirmed outcome for
        # which a text link is safe.  Unknown transport errors may have reached
        # the remote side and must not be replayed.
        if state is None:
            link_text = normalize_delivery_text(links.download_url, field="download_url")
            fallback_state = None
        else:
            fallback_state, link_text = reserve_text_message(links.download_url)
        try:
            await send_with_delivery(
                session,
                link_text,
                fallback_state,
                texts=[link_text],
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            raise DeliveryError("source archive upload was unsupported and link fallback was not confirmed") from None
        record_tool_evidence(
            {
                "artifact": artifact_metadata(artifact, include_hash=False),
                "delivery_mode": "link_fallback",
                "delivery_confirmed": True,
            }
        )
        return {
            "mode": "link_fallback",
            "link": links.download_url,
            "confirmed": True,
            "artifact_ref": artifact.artifact_ref,
            "version": artifact.version,
            "expires_at": artifact.expires_at,
        }

    try:
        await runtime.append_history(
            session.channel.id,
            "",
            "bot",
            "assistant",
            "[Sent source archive]",
        )
    except asyncio.CancelledError:
        raise
    except Exception as history_error:
        runtime.warn(f"web artifact archive history failed: {type(history_error).__name__}")
    record_tool_evidence(
        {
            "artifact": artifact_metadata(artifact, include_hash=False),
            "delivery_mode": "file",
            "delivery_confirmed": True,
        }
    )
    return {
        "mode": "file",
        "link": links.download_url,
        "confirmed": True,
        "artifact_ref": artifact.artifact_ref,
        "version": artifact.version,
        "expires_at": artifact.expires_at,
    }


def find_file_info(artifact: Artifact, path: str) -> ArtifactFileInfo:
    """Find a manifest entry without allowing arbitrary filesystem access."""

    if not isinstance(path, str):
        raise DeliveryError("artifact path is required")
    normalized = path.strip().replace("\\", "/")
    if not normalized or normalized.startswith("/") or ".." in normalized.split("/"):
        raise DeliveryError("artifact path is invalid")
    for info in artifact.files:
        if info.path == normalized:
            return info
    raise DeliveryError("artifact file was not found in the current version")


def is_text_artifact_file(info: ArtifactFileInfo, mime: str = "") -> bool:
    normalized_mime = mime or info.mime
    if normalized_mime.startswith("text/") or normalized_mime in {
        "application/json",
        "application/javascript",
        "text/javascript",
        "image/svg+xml",
    }:
        return True
    return False


def json_result(payload: Mapping[str, object]) -> str:
    return json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":"))


def normalized_limit(value: object, *, default: int, maximum: int) -> int:
    if type(value) is not int:
        return default
    return min(maximum, max(1, value))


def normalized_offset(value: object) -> int:
    if type(value) is not int or value < 0:
        raise DeliveryError("offset must be a non-negative integer")
    return value


__all__ = [
    "ArtifactToolContext",
    "AuthorizedArtifactAccess",
    "ArtifactStoreError",
    "HistoryAppender",
    "WarningSink",
    "artifact_evidence",
    "artifact_metadata",
    "build_source_file",
    "deliver_source_archive",
    "find_file_info",
    "is_text_artifact_file",
    "json_result",
    "links_payload",
    "normalized_limit",
    "normalized_offset",
    "record_artifact_evidence",
    "MAX_SOURCE_ZIP_BYTES",
    "require_authorized_access",
]
