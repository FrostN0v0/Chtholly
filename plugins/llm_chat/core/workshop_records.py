"""Bounded model-visible workshop references, never source or private reports."""

from __future__ import annotations

from collections.abc import Mapping

from .types import JSONType
from .tool_trace_safety import sanitize_json, parse_json_object

WORKSHOP_TOOLS = frozenset({"submit_plugin", "activate_plugin", "rollback_plugin"})
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
)


def project_workshop_arguments(tool_name: str, arguments: Mapping[str, object]) -> dict[str, JSONType]:
    result = {
        key: sanitize_json(arguments[key], max_text=128)
        for key in ("plugin_name", "version", "source_hash")
        if key in arguments
    }
    if tool_name == "submit_plugin":
        files = arguments.get("files")
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
    return {key: sanitize_json(parsed[key], max_text=128) for key in _SUMMARY_FIELDS if key in parsed}
