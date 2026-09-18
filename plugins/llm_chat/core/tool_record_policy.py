"""Canonical persistence policy for model-visible tool calls and results."""

from __future__ import annotations

from hashlib import sha256
from collections.abc import Mapping

from .types import JSONType
from .artifact_records import ARTIFACT_TOOLS, project_artifact_result, project_artifact_arguments
from .workshop_records import WORKSHOP_TOOLS, project_workshop_result, project_workshop_arguments
from .tool_trace_policy import _PREPARATION_TOOLS, project_prepared_result
from .tool_trace_safety import (
    safe_url,
    sanitize_json,
    external_source_type,
    redact_reference_text,
    project_message_arguments,
)

_MAX_RECORDED_TEXT = 50_000
_MAX_GENERIC_TEXT = 16_000
_PROJECTED_RESULT_TOOLS = {
    "finish_turn",
    "capture_web_reference",
    "edit_image",
    "call_plugin",
    "describe_channel_image",
    "describe_channel_participant_avatar",
    "find_channel_participants",
    "list_image_resources",
    "read_channel_messages",
    "prepare_audio",
    "prepare_channel_image",
    "prepare_image",
    "tag_image",
}


def _exact_text(value: object, limit: int = _MAX_RECORDED_TEXT) -> str:
    if not isinstance(value, str):
        return ""
    return redact_reference_text(value)[:limit]


def _text_descriptor(value: object) -> dict[str, JSONType]:
    if not isinstance(value, str):
        return {"chars": 0, "sha256": ""}
    return {
        "chars": len(value),
        "sha256": sha256(value.encode("utf-8")).hexdigest(),
    }


def record_tool_arguments(tool_name: str, arguments: Mapping[str, object]) -> dict[str, JSONType]:
    """Return the durable, model-readable subset of one tool request."""

    if tool_name == "finish_turn":
        return _record_selected(arguments, "outcome")
    if tool_name in ARTIFACT_TOOLS:
        return project_artifact_arguments(tool_name, arguments)
    if tool_name in WORKSHOP_TOOLS:
        return project_workshop_arguments(tool_name, arguments)
    if tool_name == "html2pic":
        html = _exact_text(arguments.get("html"))
        return {"html": html, "width": _safe_integer(arguments.get("width"), 900)}
    if tool_name == "markdown2pic":
        markdown = _exact_text(arguments.get("markdown"))
        return {"markdown": markdown, "width": _safe_integer(arguments.get("width"), 900)}
    if tool_name == "jinja2pic":
        return _record_selected(
            arguments,
            "title",
            "subtitle",
            "metrics",
            "columns",
            "rows",
            "notes",
            "width",
        )
    if tool_name == "prepare_external_media":
        source = arguments.get("source")
        return {
            "source_type": external_source_type(source),
            "source": safe_url(source) if external_source_type(source) == "public_url" else _text_descriptor(source),
        }
    if tool_name == "prepare_image":
        paths = arguments.get("image_paths")
        return {
            **_record_selected(arguments, "context"),
            "path_count": len(paths) if isinstance(paths, list) else 0,
        }
    if tool_name == "read_channel_messages":
        return {
            "limit": _safe_integer(arguments.get("limit"), 10),
            "filtered": bool(arguments.get("participant_ref")),
            "paged": bool(arguments.get("before_cursor")),
        }
    if tool_name in {
        "describe_channel_image",
        "describe_channel_participant_avatar",
        "find_channel_participants",
        "prepare_channel_image",
        "list_image_resources",
        "prepare_audio",
        "tag_image",
    }:
        return {"requested": True}
    if tool_name == "synthesize_speech":
        return _record_selected(
            arguments,
            "text",
            "version",
            "model_name",
            "reference_language",
            "emotion",
            "text_language",
            "speed",
        )
    if tool_name == "send_msg":
        return project_message_arguments(arguments, max_text=_MAX_GENERIC_TEXT)
    if tool_name == "prepare_merged_forward":
        return _record_selected(arguments, "messages")
    if tool_name == "read_web_page":
        return {"url": safe_url(arguments.get("url")), **_record_selected(arguments, "focus")}
    if tool_name == "capture_web_reference":
        return {
            "url": safe_url(arguments.get("url")),
            **_record_selected(arguments, "purpose", "section", "width"),
        }
    if tool_name == "web_search":
        return _record_selected(arguments, "query")
    if tool_name == "generate_image":
        return _record_selected(arguments, "prompt", "size", "use_persona_reference")
    if tool_name == "edit_image":
        references = arguments.get("reference_image_refs")
        return {
            **_record_selected(arguments, "prompt", "source_image_index", "size", "use_persona_reference"),
            "reference_count": len(references) if isinstance(references, list) else 0,
        }
    if tool_name == "call_plugin":
        command_line = arguments.get("command_line")
        command = command_line.strip().split(maxsplit=1)[0] if isinstance(command_line, str) else ""
        return {"command": command.lstrip("/.")}
    sanitized = sanitize_json(arguments, max_text=_MAX_GENERIC_TEXT)
    return sanitized if isinstance(sanitized, dict) else {}


def record_tool_result(
    tool_name: str,
    result: object,
    *,
    projected_result: JSONType | None = None,
) -> JSONType:
    """Return the durable, model-readable subset of one tool result."""

    if tool_name in _PREPARATION_TOOLS:
        if projected_result is not None:
            return sanitize_json(projected_result, max_text=2000)
        return project_prepared_result(result)
    if tool_name in ARTIFACT_TOOLS:
        return projected_result if projected_result is not None else project_artifact_result(result)
    if tool_name in WORKSHOP_TOOLS:
        return projected_result if projected_result is not None else project_workshop_result(result)
    if tool_name in _PROJECTED_RESULT_TOOLS and projected_result is not None:
        return projected_result
    return sanitize_json(result, max_text=_MAX_GENERIC_TEXT)


def _record_selected(arguments: Mapping[str, object], *names: str) -> dict[str, JSONType]:
    result: dict[str, JSONType] = {}
    for name in names:
        if name not in arguments:
            continue
        value = arguments[name]
        if isinstance(value, str):
            result[name] = _exact_text(value, _MAX_GENERIC_TEXT)
        else:
            result[name] = sanitize_json(value, max_text=_MAX_GENERIC_TEXT)
    return result


def _safe_integer(value: object, default: int) -> int:
    return value if type(value) is int else default
