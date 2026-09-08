"""publish_web_preview LLM tool implementation."""

from __future__ import annotations

from typing import Literal, TypedDict, cast
import asyncio
from collections.abc import Mapping

from arclet.entari import Session
from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from utils.web_artifacts_core import Artifact, ArtifactOwner

from ._artifacts import (
    ArtifactToolContext,
    json_result,
    links_payload,
    artifact_metadata,
    record_artifact_evidence,
    require_authorized_access,
)
from ._rendering import deliver_image_bytes
from ..core.types import JSONType
from ._registration import register_tool
from ..core.delivery import DeliveryError
from ..artifacts_runtime import ArtifactCaptureError, ArtifactCaptureUnavailable


class _ThumbnailResult(TypedDict, total=False):
    status: Literal["unavailable", "failed", "captured_not_delivered", "sent"]
    bytes: int


async def _capture_and_deliver_thumbnail(
    session: Session,
    runtime: ArtifactToolContext,
    artifact: Artifact,
    owner: ArtifactOwner,
) -> _ThumbnailResult:
    """Capture, persist, and optionally send one real PNG derivative."""

    try:
        data = await runtime.service.capture_preview(artifact, width=runtime.capture_width)
    except asyncio.CancelledError:
        raise
    except ArtifactCaptureUnavailable:
        return {"status": "unavailable"}
    except ArtifactCaptureError:
        return {"status": "failed"}
    except Exception as exc:
        runtime.warn(f"web artifact thumbnail capture failed: {type(exc).__name__}")
        return {"status": "failed"}

    try:
        await runtime.service.attach_preview(
            artifact.artifact_ref,
            owner,
            data,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        runtime.warn(f"web artifact thumbnail persistence failed: {type(exc).__name__}")
        return {"status": "failed"}

    try:
        await deliver_image_bytes(
            session,
            data,
            append_history=runtime.append_history,
            warn=runtime.warn,
            tool_name="publish_web_preview",
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        runtime.warn(f"web artifact thumbnail delivery failed: {type(exc).__name__}")
        return {"status": "captured_not_delivered", "bytes": len(data)}
    return {"status": "sent", "bytes": len(data)}


def register_publish_web_preview(
    dispatcher: PluginDispatcher[JSONType],
    runtime: ArtifactToolContext,
) -> Subscriber[JSONType]:
    """Register immutable project publication and optional real thumbnail delivery."""

    async def publish_web_preview(
        session: Session,
        title: str,
        files: list[dict[str, str]],
        entry: str = "index.html",
        previous_artifact_ref: str = "",
        delete_paths: list[str] = cast(list[str], None),
    ) -> str:
        """Create a versioned, isolated web artifact and return expiring links.

        Use only for a current user request to create, design, change, preview,
        publish, or deliver a webpage/UI/prototype.  ``files`` must contain the
        complete explicitly supplied source files as path/content/encoding
        mappings; never invent local paths or read arbitrary files.  Set
        ``previous_artifact_ref`` only to an exact artifact_ref returned by
        this tool when the user asks to revise that project, and use
        ``delete_paths`` only for files the user explicitly wants removed.
        The source is persisted before optional capture.  A capture or
        thumbnail transport failure is reported honestly while the source and
        expiring links remain valid.  Do not send link text automatically;
        return the exact links from this result through normal final text or
        call send_artifact when the user explicitly requests the ZIP.
        """

        access = require_authorized_access("publish")
        if not isinstance(title, str) or not title.strip():
            raise DeliveryError("artifact title is required")
        if not isinstance(files, list):
            raise DeliveryError("artifact files must be a list of explicit file mappings")
        if any(not isinstance(item, Mapping) for item in files):
            raise DeliveryError("artifact files must be explicit path/content mappings")
        normalized_deletes: list[str]
        if delete_paths is None:
            normalized_deletes = []
        elif isinstance(delete_paths, list) and all(isinstance(path, str) for path in delete_paths):
            normalized_deletes = delete_paths
        else:
            raise DeliveryError("delete_paths must be a list of artifact-relative paths")
        normalized_previous = previous_artifact_ref.strip() if isinstance(previous_artifact_ref, str) else ""
        if not isinstance(entry, str) or not entry.strip():
            raise DeliveryError("artifact entry is required")

        def _record_commit(committed: Artifact, effect: str) -> None:
            record_artifact_evidence(committed, artifact_effect=effect)

        artifact = await runtime.service.publish(
            access.owner,
            title,
            cast(list[Mapping[str, str]], files),
            entry=entry,
            previous_ref=normalized_previous,
            ttl_hours=runtime.service.ttl_hours,
            turn_key=access.turn_key,
            delete_paths=normalized_deletes,
            on_commit=_record_commit,
        )
        links = links_payload(runtime.service, artifact)
        thumbnail = await _capture_and_deliver_thumbnail(session, runtime, artifact, access.owner)

        record_artifact_evidence(
            artifact,
            artifact_effect="published",
            thumbnail_status=thumbnail.get("status", "unavailable"),
        )
        metadata = artifact_metadata(artifact)
        metadata.update(
            {
                "preview_url": links.preview_url,
                "download_url": links.download_url,
                "expires_at": artifact.expires_at,
                "thumbnail_status": thumbnail.get("status", "unavailable"),
                "thumbnail_bytes": thumbnail.get("bytes", 0),
                "delivery_guidance": (
                    "Deliver both exact links and the expiry after all requested media; "
                    "use send_artifact when a source ZIP is requested."
                ),
            }
        )
        return json_result(metadata)

    return register_tool(dispatcher, publish_web_preview)


__all__ = ["register_publish_web_preview"]
