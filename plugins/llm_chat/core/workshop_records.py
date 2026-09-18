"""Bounded model-visible workshop references, never source or private reports."""

from __future__ import annotations

from collections.abc import Mapping

from .types import JSONType
from .tool_trace_safety import sanitize_json, parse_json_object

WORKSHOP_TOOLS = frozenset(
    {"submit_plugin", "activate_plugin", "rollback_plugin", "list_workshop_plugins", "read_workshop_plugin"}
)
_SUMMARY_FIELDS = (
    "plugin_name",
    "title",
    "version",
    "source_hash",
    "validation_status",
    "approved",
    "active",
    "enabled",
    "active_version",
    "confirmed",
    "workshop_effect",
    "failed_checks",
    "code",
    "mode",
    "created_at",
    "offset",
    "next_offset",
    "file_count",
    "total_chars",
    "size",
    "content_sha256",
)


def project_workshop_arguments(tool_name: str, arguments: Mapping[str, object]) -> dict[str, JSONType]:
    result = {
        key: sanitize_json(arguments[key], max_text=128)
        for key in ("plugin_name", "version", "source_hash", "mode", "offset", "limit", "max_chars", "content_sha256")
        if key in arguments
    }
    if tool_name == "submit_plugin":
        files = arguments.get("source_files")
        if isinstance(files, Mapping):
            result["file_count"] = len(files)
            result["source_chars"] = sum(len(value) for value in files.values() if isinstance(value, str))
        manifest = arguments.get("manifest")
        if isinstance(manifest, Mapping):
            result["title"] = sanitize_json(manifest.get("title"), max_text=128)
    return result


def project_workshop_result(result: object) -> dict[str, JSONType]:
    parsed = parse_json_object(result)
    if parsed is None:
        return {}
    projected = {key: sanitize_json(parsed[key], max_text=128) for key in _SUMMARY_FIELDS if key in parsed}
    items = parsed.get("plugins")
    if isinstance(items, list):
        projected["plugins"] = [
            {key: sanitize_json(item[key], max_text=128) for key in _SUMMARY_FIELDS if key in item}
            for item in items[:50]
            if isinstance(item, Mapping)
        ]
        projected["returned_count"] = len(items)
    return projected
