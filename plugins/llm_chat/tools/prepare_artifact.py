"""prepare_artifact LLM tool implementation."""

from __future__ import annotations

import asyncio

from arclet.entari import Session
from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from ._artifacts import (
    ArtifactToolContext,
    json_result,
    links_payload,
    prepare_source_archive,
    require_authorized_access,
)
from ..core.types import JSONType
from ._registration import register_tool
from ..core.delivery import DeliveryError


def register_prepare_artifact(
    dispatcher: PluginDispatcher[JSONType],
    runtime: ArtifactToolContext,
) -> Subscriber[JSONType]:
    """Register immutable inline source ZIP preparation."""

    async def prepare_artifact(session: Session, artifact_ref: str) -> str:
        """Prepare the exact source ZIP for one owned artifact revision.

        Pass an exact artifact_ref returned by publish_web_preview or
        list_web_artifacts; never invent one or pass a local path or URL.
        This only prepares inline File bytes and returns a media_ref. Submit
        that reference as a standalone media segment in send_msg to deliver
        it. The returned expiring link can instead be included explicitly in
        a separate send_msg. No upload failure automatically sends the link.
        """

        access = require_authorized_access()
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
        result = prepare_source_archive(session, artifact, data, links)
        return json_result(result)

    return register_tool(dispatcher, prepare_artifact)


__all__ = ["register_prepare_artifact"]
