"""Bounded artifact audit metadata without source files or capability links."""

from __future__ import annotations

from hashlib import sha256
from collections.abc import Mapping, Sequence

from .types import JSONType
from .tool_trace_safety import safe_int, sanitize_json, parse_json_object

ARTIFACT_TOOLS = frozenset(
    {
        "publish_web_preview",
        "send_artifact",
        "list_web_artifacts",
        "read_web_artifact",
        "revoke_web_preview",
    }
)
_SUMMARY_FIELDS = (
    "artifact_ref",
    "project_ref",
    "title",
    "version",
    "created_at",
    "expires_at",
    "source_bytes",
    "zip_bytes",
    "source_sha256",
    "file_count",
    "status",
    "mode",
    "confirmed",
    "revoked",
    "thumbnail_status",
)


def _selected(value: Mapping[str, object], names: Sequence[str]) -> dict[str, JSONType]:
    return {name: sanitize_json(value[name], max_text=256) for name in names if name in value}


def project_artifact_arguments(tool_name: str, arguments: Mapping[str, object]) -> dict[str, JSONType]:
    """Keep references and bounded descriptors, never model-generated source or base64."""

    if tool_name == "publish_web_preview":
        supplied = arguments.get("files")
        files = supplied if isinstance(supplied, list) else []
        source_chars = sum(
            len(item["content"]) for item in files if isinstance(item, Mapping) and isinstance(item.get("content"), str)
        )
        deleted = arguments.get("delete_paths")
        return {
            **_selected(arguments, ("title", "previous_artifact_ref")),
            "file_count": len(files),
            "source_chars": source_chars,
            "deleted_file_count": len(deleted) if isinstance(deleted, list) else 0,
        }
    return _selected(arguments, ("artifact_ref", "limit", "offset", "max_chars"))


def project_artifact_result(result: object) -> dict[str, JSONType]:
    """Artifact storage is authoritative for source; AgentEvent retains metadata only."""

    parsed = parse_json_object(result)
    if parsed is None:
        return {}
    projected = _selected(parsed, _SUMMARY_FIELDS)
    nested = parsed.get("artifact")
    if isinstance(nested, Mapping):
        projected["artifact"] = _selected(nested, _SUMMARY_FIELDS)
    items = parsed.get("artifacts")
    if isinstance(items, list):
        projected["artifacts"] = [_selected(item, _SUMMARY_FIELDS) for item in items[:10] if isinstance(item, Mapping)]
        projected["returned_count"] = len(items)
    for name in ("offset", "next_offset", "total_chars", "size"):
        if name in parsed:
            projected[name] = None if parsed[name] is None else safe_int(parsed[name])
    content = parsed.get("content")
    if isinstance(content, str):
        projected["content_chars"] = len(content)
        projected["content_sha256"] = sha256(content.encode("utf-8")).hexdigest()
    return projected
