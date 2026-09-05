"""read_web_artifact LLM tool implementation."""

from __future__ import annotations

import asyncio

from arclet.entari import Session
from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from ._artifacts import (
    ArtifactToolContext,
    json_result,
    find_file_info,
    artifact_metadata,
    normalized_offset,
    is_text_artifact_file,
    require_authorized_access,
)
from ..core.types import JSONType
from ._registration import register_tool
from ..core.delivery import DeliveryError
from ..core.tool_trace import record_tool_evidence

MAX_READ_CHARS = 16_000


def register_read_web_artifact(
    dispatcher: PluginDispatcher[JSONType],
    runtime: ArtifactToolContext,
) -> Subscriber[JSONType]:
    """Register bounded source reads against an exact owned revision."""

    async def read_web_artifact(
        session: Session,
        artifact_ref: str,
        path: str = "index.html",
        offset: int = 0,
        max_chars: int = MAX_READ_CHARS,
    ) -> str:
        """Read text source or binary metadata from one artifact revision.

        Use after the current user explicitly asks to inspect/read the source
        or a file.  ``artifact_ref`` must be an exact ref returned by the
        artifact tools; ``path`` is resolved only against that immutable
        manifest.  Text is returned in bounded character windows with
        ``next_offset`` for continuation.  Binary files return MIME, size, and
        hash metadata only; bytes are never base64 encoded into chat context.
        """

        del session
        access = require_authorized_access("read")
        if not isinstance(artifact_ref, str) or not artifact_ref.strip():
            raise DeliveryError("artifact_ref is required")
        if not isinstance(path, str) or not path.strip():
            raise DeliveryError("artifact path is required")
        start = normalized_offset(offset)
        if type(max_chars) is not int or max_chars < 1:
            raise DeliveryError("max_chars must be a positive integer")
        window = min(MAX_READ_CHARS, max_chars)
        normalized_ref = artifact_ref.strip()

        try:
            artifact = await runtime.service.get_owned(
                normalized_ref,
                access.owner,
                admin=access.is_operator,
            )
            info = find_file_info(artifact, path)
            data, mime = await runtime.service.read_owned_file(
                normalized_ref,
                access.owner,
                str(getattr(info, "path", path)),
                admin=access.is_operator,
            )
        except asyncio.CancelledError:
            raise
        except DeliveryError:
            raise
        except Exception as exc:
            runtime.warn(f"web artifact source read failed: {type(exc).__name__}")
            raise DeliveryError("the requested artifact file is unavailable") from None

        source_path = str(getattr(info, "path", path))
        metadata = artifact_metadata(artifact, include_hash=False)
        metadata.update({"path": source_path, "mime": mime})
        if not is_text_artifact_file(info, mime):
            metadata.update(
                {
                    "binary": True,
                    "size": len(data),
                    "sha256": str(getattr(info, "sha256", "")),
                    "encoding": str(getattr(info, "encoding", "base64") or "base64"),
                    "content": None,
                    "next_offset": None,
                }
            )
            record_tool_evidence({"artifact": artifact_metadata(artifact, include_hash=False), "binary": True})
            return json_result(metadata)

        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            metadata.update(
                {
                    "binary": True,
                    "size": len(data),
                    "sha256": str(getattr(info, "sha256", "")),
                    "encoding": str(getattr(info, "encoding", "base64") or "base64"),
                    "content": None,
                    "next_offset": None,
                }
            )
            record_tool_evidence({"artifact": artifact_metadata(artifact, include_hash=False), "binary": True})
            return json_result(metadata)

        chunk = text[start : start + window]
        next_offset = start + len(chunk) if start + len(chunk) < len(text) else None
        metadata.update(
            {
                "binary": False,
                "encoding": "utf-8",
                "content": chunk,
                "offset": start,
                "next_offset": next_offset,
                "truncated": next_offset is not None,
            }
        )
        record_tool_evidence({"artifact": artifact_metadata(artifact, include_hash=False), "binary": False})
        return json_result(metadata)

    return register_tool(dispatcher, read_web_artifact)


__all__ = ["MAX_READ_CHARS", "register_read_web_artifact"]
