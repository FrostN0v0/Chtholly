"""publish_web_preview LLM tool implementation."""

from __future__ import annotations

from typing import cast
import asyncio
from collections.abc import Mapping

from pydantic import ValidationError
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
from ._rendering import prepare_image_bytes
from ..core.types import JSONType
from ._registration import register_tool
from ..core.delivery import DeliveryError
from ..artifacts_runtime import ArtifactCaptureError, ArtifactCaptureUnavailable, _wait_for_task
from ._submission_models import WebSourceFile


async def _capture_and_prepare_thumbnail(
    session: Session,
    runtime: ArtifactToolContext,
    artifact: Artifact,
    owner: ArtifactOwner,
) -> dict[str, JSONType]:
    """Capture, persist, and prepare a PNG without delivering or revoking it."""

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
        return await prepare_image_bytes(
            session,
            data,
            warn=runtime.warn,
            tool_name="publish_web_preview",
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        runtime.warn(f"web artifact thumbnail preparation failed: {type(exc).__name__}")
        return {"status": "captured_not_prepared", "bytes": len(data)}


def register_publish_web_preview(
    dispatcher: PluginDispatcher[JSONType],
    runtime: ArtifactToolContext,
) -> Subscriber[JSONType]:
    """Register immutable publication and optional real thumbnail preparation."""

    async def publish_web_preview(
        session: Session,
        title: str,
        source_files: list[WebSourceFile],
        entry: str = "index.html",
        previous_artifact_ref: str = "",
        delete_paths: list[str] = cast(list[str], None),
    ) -> str:
        """Create a versioned, isolated web artifact and return expiring links.

        Choose this workflow when a working webpage/UI/prototype helps fulfill
        the user's task, including contextual follow-ups.  No special wording
        or separate publication command is needed.  ``source_files`` is an array
        of objects with the named fields path, content and optional encoding;
        for example [{"path":"index.html","content":"<h1>Hello</h1>"}].
        Never use filename-to-content objects here, read arbitrary local files,
        or publish secrets/private chat data. All arguments are top-level fields.
        Use an exact ``previous_artifact_ref`` from the artifact tools for
        revisions and ``delete_paths`` for inherited files omitted from the
        new version; existing versions remain unchanged.  Anyone holding the
        returned links can access the project until expiry or revocation.
        Capture failures leave the source and links valid. Use send_msg to
        deliver exact links, expiry, and any prepared thumbnail in your chosen
        order. Use prepare_artifact then send_msg for a source ZIP. Publication
        alone does not deliver a message; unused thumbnails do not revoke it.
        """

        access = require_authorized_access()
        if not isinstance(title, str) or not title.strip():
            raise DeliveryError("artifact title is required")
        if not isinstance(source_files, list):
            raise DeliveryError("artifact files must be a list of explicit file mappings")
        try:
            sources = [WebSourceFile.model_validate(item).model_dump() for item in source_files]
        except ValidationError:
            raise DeliveryError(
                "source_files must be an array of objects with string path and content fields plus optional "
                "encoding (utf-8 or base64), not filename-to-content objects"
            ) from None
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
            cast(list[Mapping[str, str]], sources),
            entry=entry,
            previous_ref=normalized_previous,
            ttl_hours=runtime.service.ttl_hours,
            turn_key=access.turn_key,
            delete_paths=normalized_deletes,
            on_commit=_record_commit,
        )
        try:
            links = links_payload(runtime.service, artifact)
            thumbnail = await _capture_and_prepare_thumbnail(session, runtime, artifact, access.owner)
        except asyncio.CancelledError:
            compensation = asyncio.create_task(runtime.service.revoke(artifact.artifact_ref, access.owner))
            try:
                revoked = await _wait_for_task(compensation, propagate_cancellation=False)
            except Exception as exc:
                runtime.warn(f"web artifact interrupted publication cleanup failed: {type(exc).__name__}")
            else:
                if revoked:
                    record_artifact_evidence(artifact, artifact_effect="revoked")
            raise

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
                "thumbnail": thumbnail,
                "delivery_guidance": (
                    "Use send_msg to deliver the exact links, expiry and optional thumbnail in your chosen order; "
                    "use prepare_artifact then a standalone media send_msg for the source ZIP."
                ),
            }
        )
        return json_result(metadata)

    return register_tool(dispatcher, publish_web_preview)


__all__ = ["register_publish_web_preview"]
