"""Per-tool retention policy for execution traces."""

from __future__ import annotations

from typing import Literal, cast
from hashlib import sha256
from dataclasses import dataclass
from collections.abc import Mapping, Sequence

from .types import JSONType
from .errors import summarize_exception
from .tool_trace_safety import (
    MAX_RESULT_TEXT,
    MAX_WEB_SOURCES,
    MAX_ARGUMENT_TEXT,
    MAX_SOURCE_SNIPPET,
    safe_int,
    safe_url,
    text_length,
    compact_text,
    sanitize_json,
    parse_json_object,
    selected_arguments,
    external_source_type,
)

ToolStatus = Literal["succeeded", "failed", "rejected", "cancelled"]
ToolEffect = Literal["observed", "confirmed", "partial", "none", "unknown"]

_DELIVERY_TOOLS = {
    "send_audio",
    "send_external_image",
    "send_channel_image",
    "send_image",
    "send_merged_forward",
    "send_text",
    "speak",
}
_OBSERVATION_TOOLS = {
    "describe_channel_participant_avatar",
    "describe_channel_image",
    "find_channel_participants",
    "get_local_time",
    "list_image_resources",
    "list_tts_voices",
    "read_channel_messages",
    "read_web_page",
    "web_search",
}


@dataclass(frozen=True, slots=True)
class DeliverySnapshot:
    """Minimal confirmed-delivery counters surrounding one tool call."""

    active: bool = False
    attempts: int = 0
    confirmed: int = 0
    confirmed_media: int = 0


def project_tool_arguments(tool_name: str, arguments: Mapping[str, object]) -> dict[str, JSONType]:
    """Retain only parameters needed for later continuity."""

    if tool_name == "web_search":
        return selected_arguments(arguments, "query")
    if tool_name == "read_web_page":
        return {**selected_arguments(arguments, "focus"), "url": safe_url(arguments.get("url"))}
    if tool_name == "get_local_time":
        return selected_arguments(arguments, "timezone")
    if tool_name == "list_image_resources":
        return selected_arguments(arguments, "limit", "offset")
    if tool_name == "list_tts_voices":
        return selected_arguments(arguments, "refresh")
    if tool_name == "find_channel_participants":
        return {
            "query_chars": text_length(arguments.get("query")),
            **selected_arguments(arguments, "limit"),
        }
    if tool_name == "read_channel_messages":
        return {
            **selected_arguments(arguments, "limit"),
            "filtered": bool(arguments.get("participant_ref")),
            "paged": bool(arguments.get("before_cursor")),
        }
    if tool_name == "describe_channel_image":
        return {"requested": bool(arguments.get("image_ref"))}
    if tool_name == "describe_channel_participant_avatar":
        return {"requested": bool(arguments.get("participant_ref"))}
    if tool_name == "send_text":
        mentions = arguments.get("mentions")
        normalized_mentions = (
            mentions if isinstance(mentions, Sequence) and not isinstance(mentions, (str, bytes)) else ()
        )
        return {
            "text_chars": text_length(arguments.get("text")),
            "mention_count": min(3, len(normalized_mentions)),
            **selected_arguments(arguments, "delay_seconds"),
        }
    if tool_name == "send_merged_forward":
        messages = arguments.get("messages")
        normalized = messages if isinstance(messages, Sequence) and not isinstance(messages, (str, bytes)) else ()
        return {
            "message_count": len(normalized),
            "total_chars": sum(text_length(message) for message in normalized),
            **selected_arguments(arguments, "delay_seconds"),
        }
    if tool_name == "send_image":
        paths = arguments.get("image_paths")
        path_count = len(paths) if isinstance(paths, Sequence) and not isinstance(paths, (str, bytes)) else 0
        return {
            "selection_mode": "paths" if path_count else "context",
            "path_count": path_count,
            "context": compact_text(arguments.get("context"), MAX_ARGUMENT_TEXT) if not path_count else "",
        }
    if tool_name == "send_external_image":
        source = arguments.get("source")
        return {"source_type": external_source_type(source), "source_chars": text_length(source)}
    if tool_name == "send_channel_image":
        return {"requested": bool(arguments.get("image_ref"))}
    if tool_name == "send_audio":
        return selected_arguments(arguments, "context")
    if tool_name == "speak":
        projected = selected_arguments(
            arguments,
            "version",
            "model_name",
            "reference_language",
            "emotion",
            "text_language",
            "speed",
        )
        projected["text_chars"] = text_length(arguments.get("text"))
        return projected
    if tool_name == "call_plugin":
        command_line = compact_text(arguments.get("command_line"), MAX_ARGUMENT_TEXT)
        normalized = command_line.lstrip("/.").split(maxsplit=1)
        return {"command": normalized[0] if normalized else ""}
    if tool_name == "tag_image":
        return selected_arguments(arguments, "image_index")
    sanitized = sanitize_json(arguments, max_text=MAX_ARGUMENT_TEXT)
    return cast(dict[str, JSONType], sanitized) if isinstance(sanitized, dict) else {}


def project_tool_success(
    tool_name: str,
    result: object,
    *,
    before: DeliverySnapshot,
    after: DeliverySnapshot,
) -> tuple[ToolStatus, ToolEffect, dict[str, JSONType]]:
    """Normalize one handler return into execution and effect semantics."""

    outcome = _project_tool_result(tool_name, result, before=before, after=after)
    if tool_name == "list_tts_voices" and outcome.get("available") is False:
        return "failed", "none", outcome
    if tool_name == "call_plugin" and _command_was_rejected(result):
        return "rejected", "none", outcome
    if tool_name in _DELIVERY_TOOLS:
        effect = delivery_effect(before, after, terminal_status="succeeded")
        if before.active and effect == "none":
            return "failed", effect, outcome
        return "succeeded", effect, outcome
    if tool_name in _OBSERVATION_TOOLS:
        return "succeeded", "observed", outcome
    if tool_name == "tag_image":
        return "succeeded", "confirmed", outcome
    return "succeeded", "unknown", outcome


def classify_tool_error(exc: BaseException, *, delivery_attempted: bool) -> tuple[ToolStatus, str]:
    """Map sanitized runtime failures to stable status and error codes."""

    summary = summarize_exception(exc).casefold()
    if "budget exhausted" in summary:
        return "rejected", "budget_exhausted"
    if delivery_attempted:
        return "failed", "delivery_failed"
    if "timed out" in summary or "timeout" in summary:
        return "failed", "timeout"
    if "unavailable" in summary or "service" in summary and "failed" in summary:
        return "failed", "service_unavailable"
    padded = f" {summary} "
    rejection_markers = (
        " must ",
        " required",
        " invalid",
        "unknown iana timezone",
        "not allowed",
        "outside llm_chat",
        "provide exactly",
        "exceeds the configured",
    )
    if any(marker in padded for marker in rejection_markers):
        return "rejected", "invalid_request"
    return "failed", "execution_failed"


def delivery_effect(
    before: DeliverySnapshot,
    after: DeliverySnapshot,
    *,
    terminal_status: ToolStatus,
) -> ToolEffect:
    """Derive confirmed or partial effects from authoritative delivery counters."""

    if not before.active or not after.active:
        return "unknown"
    confirmed_delta = max(0, after.confirmed - before.confirmed)
    if confirmed_delta == 0:
        return "none"
    return "confirmed" if terminal_status == "succeeded" else "partial"


def tool_error_effect(
    tool_name: str,
    before: DeliverySnapshot,
    after: DeliverySnapshot,
    *,
    terminal_status: ToolStatus,
) -> ToolEffect:
    """Return partial delivery only for side-effect tools with confirmed prefixes."""

    if tool_name not in _DELIVERY_TOOLS:
        return "none"
    return delivery_effect(before, after, terminal_status=terminal_status)


def _project_tool_result(
    tool_name: str,
    result: object,
    *,
    before: DeliverySnapshot,
    after: DeliverySnapshot,
) -> dict[str, JSONType]:
    if tool_name == "web_search" and isinstance(result, Mapping):
        raw_results = result.get("results")
        result_items = (
            raw_results if isinstance(raw_results, Sequence) and not isinstance(raw_results, (str, bytes)) else ()
        )
        sources: list[JSONType] = []
        for raw_item in result_items[:MAX_WEB_SOURCES]:
            if isinstance(raw_item, Mapping):
                sources.append(
                    {
                        "title": compact_text(raw_item.get("title"), MAX_ARGUMENT_TEXT),
                        "url": compact_text(raw_item.get("url"), MAX_ARGUMENT_TEXT),
                        "snippet": compact_text(raw_item.get("snippet"), MAX_SOURCE_SNIPPET),
                    }
                )
        return {
            "query": compact_text(result.get("query"), MAX_ARGUMENT_TEXT),
            "result_count": len(result_items),
            "sources": sources,
        }
    if tool_name == "read_web_page" and isinstance(result, Mapping):
        raw_content = result.get("content")
        content = raw_content if isinstance(raw_content, str) else ""
        return {
            "url": compact_text(result.get("url"), MAX_ARGUMENT_TEXT),
            "content_chars": len(content),
            "content_hash": sha256(content.encode("utf-8")).hexdigest()[:16] if content else "",
            "excerpt": compact_text(content, MAX_RESULT_TEXT),
        }
    parsed = parse_json_object(result)
    if tool_name == "get_local_time" and parsed is not None:
        return {
            key: sanitize_json(parsed.get(key), max_text=MAX_ARGUMENT_TEXT)
            for key in ("timezone", "datetime", "date", "time", "weekday", "utc_offset")
            if key in parsed
        }
    if tool_name == "list_image_resources" and parsed is not None:
        images = parsed.get("images")
        return {
            "total": safe_int(parsed.get("total")),
            "offset": safe_int(parsed.get("offset")),
            "returned_count": (
                len(images) if isinstance(images, Sequence) and not isinstance(images, (str, bytes)) else 0
            ),
        }
    if tool_name == "list_tts_voices" and parsed is not None:
        return _project_tts_catalog(parsed)
    if tool_name == "find_channel_participants" and parsed is not None:
        participants = parsed.get("participants")
        return {
            "returned_count": (
                len(participants)
                if isinstance(participants, Sequence) and not isinstance(participants, (str, bytes))
                else 0
            )
        }
    if tool_name == "read_channel_messages" and parsed is not None:
        messages = parsed.get("messages")
        normalized_messages = (
            messages if isinstance(messages, Sequence) and not isinstance(messages, (str, bytes)) else ()
        )
        image_count = sum(
            safe_int(message.get("image_count")) for message in normalized_messages if isinstance(message, Mapping)
        )
        return {
            "returned_count": len(normalized_messages),
            "image_count": image_count,
            "has_older": bool(parsed.get("next_cursor")),
            "truncated": parsed.get("truncated") is True,
        }
    if tool_name == "describe_channel_image" and parsed is not None:
        return {
            "available": parsed.get("available") is True,
            "reason": compact_text(parsed.get("reason"), MAX_ARGUMENT_TEXT),
            "description_chars": text_length(parsed.get("description")),
        }
    if tool_name == "describe_channel_participant_avatar" and parsed is not None:
        return {
            "available": parsed.get("available") is True,
            "reason": compact_text(parsed.get("reason"), MAX_ARGUMENT_TEXT),
        }
    if tool_name in _DELIVERY_TOOLS:
        return {
            "confirmed_deliveries": max(0, after.confirmed - before.confirmed),
            "confirmed_media_deliveries": max(0, after.confirmed_media - before.confirmed_media),
            "summary": compact_text(result, MAX_ARGUMENT_TEXT),
        }
    return {"summary": compact_text(result, MAX_RESULT_TEXT)}


def _project_tts_catalog(parsed: Mapping[str, object]) -> dict[str, JSONType]:
    voices = parsed.get("voices")
    model_names: list[JSONType] = []
    if isinstance(voices, Sequence) and not isinstance(voices, (str, bytes)):
        for voice in voices[:10]:
            if isinstance(voice, Mapping):
                name = compact_text(voice.get("model_name"), MAX_ARGUMENT_TEXT)
                if name:
                    model_names.append(name)
    return {
        "available": parsed.get("available", True) is not False,
        "provider": compact_text(parsed.get("provider"), MAX_ARGUMENT_TEXT),
        "voice_count": len(voices) if isinstance(voices, Sequence) and not isinstance(voices, (str, bytes)) else 0,
        "model_names": model_names,
    }


def _command_was_rejected(result: object) -> bool:
    return isinstance(result, str) and "\u4e0d\u5728\u5141\u8bb8\u5217\u8868\u4e2d" in result
