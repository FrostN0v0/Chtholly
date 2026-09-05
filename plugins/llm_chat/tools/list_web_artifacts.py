"""list_web_artifacts LLM tool implementation."""

from __future__ import annotations

from arclet.entari import Session
from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from ._artifacts import (
    ArtifactToolContext,
    json_result,
    normalized_limit,
    artifact_metadata,
    require_authorized_access,
)
from ..core.types import JSONType
from ._registration import register_tool
from ..core.delivery import DeliveryError
from ..core.tool_trace import record_tool_evidence


def register_list_web_artifacts(
    dispatcher: PluginDispatcher[JSONType],
    runtime: ArtifactToolContext,
) -> Subscriber[JSONType]:
    """Register owner/scope-bound active artifact listing."""

    async def list_web_artifacts(session: Session, limit: int = 10) -> str:
        """List active artifact revisions in the current scope.

        Use after the current user asks to list their web projects, previews,
        or versions.  Results contain opaque artifact refs and bounded metadata
        only; public capability tokens, source content, local paths, and file
        maps are never returned.  Operators can manage other users only inside
        the current scope.
        """

        del session
        access = require_authorized_access("list")
        limit_value = normalized_limit(limit, default=10, maximum=10)
        try:
            artifacts = await runtime.service.list_owned(
                access.owner,
                admin=access.is_operator,
                limit=limit_value,
            )
        except Exception as exc:
            runtime.warn(f"web artifact listing failed: {type(exc).__name__}")
            raise DeliveryError("web artifact list is unavailable") from None

        items = [artifact_metadata(artifact, include_hash=False) for artifact in artifacts]
        record_tool_evidence({"artifacts": items, "returned_count": len(items)})
        return json_result({"artifacts": items, "limit": limit_value})

    return register_tool(dispatcher, list_web_artifacts)


__all__ = ["register_list_web_artifacts"]
