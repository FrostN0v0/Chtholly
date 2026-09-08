"""revoke_web_preview LLM tool implementation."""

from __future__ import annotations

import asyncio

from arclet.entari import Session
from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from ._artifacts import (
    ArtifactToolContext,
    json_result,
    record_artifact_evidence,
    require_authorized_access,
)
from ..core.types import JSONType
from ._registration import register_tool
from ..core.delivery import DeliveryError
from ..core.tool_trace import record_tool_evidence
from ..core.artifact_access import ArtifactAccessError, require_artifact_revocation


def register_revoke_web_preview(
    dispatcher: PluginDispatcher[JSONType],
    runtime: ArtifactToolContext,
) -> Subscriber[JSONType]:
    """Register immediate scoped preview revocation without chat delivery."""

    async def revoke_web_preview(session: Session, artifact_ref: str) -> str:
        """Revoke one owned artifact revision and all public capabilities.

        Use only after the current user explicitly asks to revoke, invalidate,
        delete, or disable a preview/link.  Pass the exact artifact_ref from
        the current artifact tools.  Operators may revoke another owner only
        inside the current scope.  This operation never sends a chat message.
        """

        del session
        access = require_authorized_access()
        try:
            require_artifact_revocation(access.raw_user_text)
        except ArtifactAccessError as exc:
            raise DeliveryError(f"artifact revocation not allowed: {exc}") from None
        if not isinstance(artifact_ref, str) or not artifact_ref.strip():
            raise DeliveryError("artifact_ref is required")
        normalized_ref = artifact_ref.strip()
        artifact = None
        try:
            artifact = await runtime.service.get_owned(
                normalized_ref,
                access.owner,
                admin=access.is_operator,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Revoke remains idempotent for an already-revoked own artifact;
            # the store performs the authoritative scoped check below.
            artifact = None
        try:
            revoked = await runtime.service.revoke(
                normalized_ref,
                access.owner,
                admin=access.is_operator,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            runtime.warn(f"web artifact revoke failed: {type(exc).__name__}")
            raise DeliveryError("the requested artifact could not be revoked") from None

        if artifact is not None:
            record_artifact_evidence(artifact, artifact_effect="revoked", revoked=revoked)
        else:
            # Repeated own revocations may have no active row left to project;
            # retain only the opaque reference and confirmed effect.
            record_tool_evidence(
                {
                    "artifact_effect": "revoked",
                    "artifact": {"artifact_ref": normalized_ref},
                    "revoked": revoked,
                }
            )
        return json_result({"artifact_ref": normalized_ref, "revoked": revoked, "confirmed": True})

    return register_tool(dispatcher, revoke_web_preview)


__all__ = ["register_revoke_web_preview"]
