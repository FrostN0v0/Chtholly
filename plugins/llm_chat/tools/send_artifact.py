"""send_artifact LLM tool implementation."""

from __future__ import annotations

import asyncio

from arclet.entari import Session
from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from ._artifacts import (
    ArtifactToolContext,
    json_result,
    links_payload,
    deliver_source_archive,
    require_authorized_access,
)
from ..core.types import JSONType
from ._registration import register_tool
from ..core.delivery import DeliveryError


def register_send_artifact(
    dispatcher: PluginDispatcher[JSONType],
    runtime: ArtifactToolContext,
) -> Subscriber[JSONType]:
    """Register confirmed inline source ZIP delivery."""

    async def send_artifact(session: Session, artifact_ref: str) -> str:
        """Send the exact source ZIP for one owned artifact revision.

        Use only after the current user explicitly asks for source, a ZIP,
        files, or the website deliverable.  Pass an exact opaque
        ``artifact_ref`` returned by publish_web_preview or list_web_artifacts;
        never invent one and never pass a local path or URL.  The archive is
        sent as inline Satori File bytes.  If the adapter explicitly reports
        that file upload is unsupported before confirmation, this tool sends
        the exact expiring ZIP link instead.  Unknown transport failures are
        not replayed and are reported without claiming delivery.
        """

        access = require_authorized_access("send")
        if not isinstance(artifact_ref, str) or not artifact_ref.strip():
            raise DeliveryError("artifact_ref is required")
        normalized_ref = artifact_ref.strip()
        try:
            artifact = await runtime.service.get_owned(
                normalized_ref,
                access.owner,
                admin=access.is_operator,
            )
            data = await runtime.service.zip_owned(
                normalized_ref,
                access.owner,
                admin=access.is_operator,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            runtime.warn(f"web artifact source read failed: {type(exc).__name__}")
            raise DeliveryError("the requested artifact revision is unavailable") from None

        links = links_payload(runtime.service, artifact)
        # Storage/read success is not delivery confirmation.  Only the helper
        # records artifact delivery evidence after the transport confirms a
        # file or its narrow safe link fallback.
        result = await deliver_source_archive(session, runtime, artifact, data, links)
        result.update(
            {
                "title": str(getattr(artifact, "title", "")),
                "zip_bytes": int(getattr(artifact, "zip_bytes", len(data)) or len(data)),
            }
        )
        return json_result(result)

    return register_tool(dispatcher, send_artifact)


__all__ = ["register_send_artifact"]
