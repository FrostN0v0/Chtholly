"""Framework-free, explicitly bounded serialization for private model audit records."""

from __future__ import annotations

import re
import json
import math
from typing import Any
from dataclasses import field, dataclass
from collections.abc import Mapping

ADMIN_ONLY_EVENT_TYPES = frozenset(
    {"model_request", "model_response", "context_snapshot", "turn_timing", "message_delivery"}
)
_SECRET_KEY = re.compile(
    r"(?:api[_-]?key|authorization|password|passwd|secret|credential|cookie|headers?|(?:access|refresh|capture)[_-]?token|bearer|private[_-]?key)",
    re.I,
)
_PRIVATE_KEY = re.compile(
    r"(?:reasoning_content|reasoning_details|thinking|chain_of_thought|thought_signature|encrypted_content|signature|b64_json|base64|image_url|image_data|image_bytes|image_paths|pixels|audio_data|file_path|local_path|inline_data|inlineData|file_data|fileData|(?:channel_|source_|reference_)?image_refs?|reference_image|participant_refs?|before_cursor)",
    re.I,
)
_SECRET_TEXT = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{8,}|AIza[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{16,}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|Bearer\s+[A-Za-z0-9._~+/-]+=*)",
    re.I,
)
_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:api[_-]?key|password|passwd|secret|authorization|(?:access|refresh|capture)[_-]?token|cookie)[\"']?\s*[=:]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;&}]+)"
)
_PRIVATE_TEXT = re.compile(
    r"(?is)<(?:think|thinking|analysis|reasoning)\b[^>]*>.*?</(?:think|thinking|analysis|reasoning)\s*>|(?:data|internal):[^\s\"'<>]+|(?:file|base64|attachment|local)://[^\s\"'<>]+|(?<![\w])[A-Za-z]:[\\/][^\s\"'<>]+|https?://[^\s\"'<>]*(?:multimedia\.nt\.qq\.com|gchat\.qpic\.cn|c2cpicdw\.qpic\.cn)[^\s\"'<>]*"
)
_LOCAL_PATH = re.compile(r"(?<![\w:/])/(?:home|root|tmp|var|opt|Users|mnt|etc|private|workspace|data)/[^\s\"'<>]+")
_BASE64_TEXT = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{256,}={0,2}(?![A-Za-z0-9+/])")
_URL_CREDENTIALS = re.compile(r"(https?://)[^\s/@:]+:[^\s/@]+@", re.I)
_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:key|api_key|token|access_token|auth|signature|sig|credential|x-amz-[^=&#]+)=)[^&#\s\"'<>]+"
)
_IMAGE_URL = re.compile(r"https?://[^\s\"'<>]+\.(?:png|jpe?g|gif|webp|bmp|avif)(?:\?[^\s\"'<>]*)?", re.I)
_UNCLOSED_THOUGHT = re.compile(r"(?is)<(?:think|thinking|analysis|reasoning)\b[^>]*>.*$")
_CAPABILITY_URL = re.compile(r"https?://[^\s/\"'<>]+/p/[A-Za-z0-9_-]{20,}[^\s\"'<>]*", re.I)


def _field(value: object, name: str) -> object:
    return value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)


def normalize_usage(value: object) -> dict[str, int | None]:
    """Normalize reported token counters, leaving missing values unknown, not zero."""

    def count(*names: str) -> int | None:
        for name in names:
            candidate = _field(value, name)
            if type(candidate) is int and candidate >= 0:
                return candidate
        return None

    result = {
        "input_tokens": count("input_tokens", "prompt_tokens", "promptTokenCount"),
        "output_tokens": count("output_tokens", "completion_tokens", "candidatesTokenCount"),
        "total_tokens": count("total_tokens", "totalTokenCount"),
        "cached_input_tokens": count(
            "cached_input_tokens", "cache_read_input_tokens", "cache_read_tokens", "cachedContentTokenCount"
        ),
        "reasoning_tokens": count("reasoning_tokens", "thoughtsTokenCount"),
    }
    for detail_name, field_name, target in (
        ("prompt_tokens_details", "cached_tokens", "cached_input_tokens"),
        ("input_tokens_details", "cached_tokens", "cached_input_tokens"),
        ("completion_tokens_details", "reasoning_tokens", "reasoning_tokens"),
        ("output_tokens_details", "reasoning_tokens", "reasoning_tokens"),
    ):
        detail = _field(_field(value, detail_name), field_name)
        if result[target] is None and type(detail) is int and detail >= 0:
            result[target] = detail
    if result["total_tokens"] is None and result["input_tokens"] is not None and result["output_tokens"] is not None:
        result["total_tokens"] = result["input_tokens"] + result["output_tokens"]
    return result


@dataclass
class _Snapshot:
    remaining: int = 2_000_000
    nodes: int = 100_000
    redactions: list[dict[str, object]] = field(default_factory=list)
    overflow: bool = False
    active: set[int] = field(default_factory=set)

    def omit(self, path: str, kind: str, value: object) -> dict[str, object]:
        item: dict[str, object] = {"path": path, "type": kind}
        if isinstance(value, (str, bytes, list, tuple, dict)):
            item["size"] = len(value)
        self.redactions.append(item)
        return {"omitted": kind, **({"size": item["size"]} if "size" in item else {})}

    def text(self, value: str, path: str) -> object:
        if len(value) > self.remaining:
            self.overflow = True
            return self.omit(path, "size_limit", value)
        self.remaining -= len(value)
        stripped = value.lstrip()
        if stripped.startswith(("{", "[")):
            try:
                parsed = json.loads(value)
            except (ValueError, RecursionError):
                pass
            else:
                before = len(self.redactions)
                safe = self.visit(parsed, path + ".json")
                if len(self.redactions) != before:
                    return json.dumps(safe, ensure_ascii=False, separators=(",", ":"))
                return value
        for pattern, kind, replacement in (
            (_PRIVATE_TEXT, "private_content", "[REDACTED]"),
            (_UNCLOSED_THOUGHT, "private_content", "[REDACTED]"),
            (_LOCAL_PATH, "local_path", "[REDACTED]"),
            (_IMAGE_URL, "image_url", "[REDACTED]"),
            (_CAPABILITY_URL, "capability_url", "[REDACTED]"),
            (_SECRET_TEXT, "credential", "[REDACTED]"),
            (_ASSIGNMENT, "credential", r"\1[REDACTED]"),
            (_URL_CREDENTIALS, "credential", r"\1[REDACTED]@"),
            (_QUERY_SECRET, "credential", r"\1[REDACTED]"),
            (_BASE64_TEXT, "encoded_data", "[REDACTED]"),
        ):
            value, count = pattern.subn(replacement, value)
            if count:
                self.redactions.append({"path": path, "type": kind, "count": count})
        return value

    def visit(self, value: object, path: str = "$", depth: int = 0, schema: bool = False) -> Any:
        self.nodes -= 1
        if self.nodes < 0 or depth > 40 or self.remaining < 0:
            self.overflow = True
            return self.omit(path, "structure_limit", value)
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else self.omit(path, "non_finite_number", value)
        if isinstance(value, str):
            return self.text(value, path)
        if isinstance(value, Mapping) and not schema:
            block_type = value.get("type")
            if (
                value.get("thought") is True
                or value.get("channel") == "analysis"
                or value.get("role") == "analysis"
                or (
                    isinstance(block_type, str)
                    and block_type
                    in {
                        "thinking",
                        "redacted_thinking",
                        "reasoning",
                        "image",
                        "image_url",
                        "input_image",
                        "input_audio",
                    }
                )
            ):
                return self.omit(path, "private_content", value)
        if isinstance(value, (Mapping, list, tuple)):
            identity = id(value)
            if identity in self.active:
                return self.omit(path, "cycle", value)
            self.active.add(identity)
            try:
                if isinstance(value, Mapping):
                    result: dict[str, object] = {}
                    for key, child in value.items():
                        if self.nodes < 0:
                            result["__audit_overflow__"] = self.omit(path, "structure_limit", value)
                            self.overflow = True
                            break
                        if not isinstance(key, str):
                            self.omit(path, "unsupported_key", key)
                            continue
                        safe_key = self.text(key, f"{path}.<key>")
                        if not isinstance(safe_key, str):
                            continue
                        child_path = f"{path}.{safe_key}"
                        is_schema = schema or (
                            key in {"parameters", "input_schema"} and ("tools" in path or "functions" in path)
                        )
                        if not schema and (_SECRET_KEY.search(key) or key.casefold() == "token"):
                            result[safe_key] = self.omit(child_path, "credential", child)
                        elif schema and key in {"default", "example", "examples"} and _SECRET_KEY.search(path):
                            result[safe_key] = self.omit(child_path, "credential", child)
                        elif not schema and (
                            _PRIVATE_KEY.fullmatch(key)
                            or key in {"preview_url", "download_url"}
                            or (key == "content" and value.get("encoding") == "base64")
                            or (key in {"analysis", "reasoning"} and isinstance(child, (str, list)))
                        ):
                            result[safe_key] = self.omit(child_path, "private_content", child)
                        else:
                            result[safe_key] = self.visit(child, child_path, depth + 1, is_schema)
                    return result
                result_list = []
                for index, child in enumerate(value):
                    if self.nodes < 0:
                        result_list.append(self.omit(path, "structure_limit", value))
                        self.overflow = True
                        break
                    result_list.append(self.visit(child, f"{path}[{index}]", depth + 1, schema))
                return result_list
            finally:
                self.active.remove(identity)
        return self.omit(
            path, "binary" if isinstance(value, (bytes, bytearray, memoryview)) else "unsupported_object", value
        )

    @property
    def status(self) -> str:
        return "overflow" if self.overflow else "redacted" if self.redactions else "complete"


def sanitize_audit_value(value: object) -> dict[str, object]:
    """Preserve safe JSON with an explicit manifest for every omission."""
    snapshot = _Snapshot()
    data = snapshot.visit(value)
    return {"data": data, "capture_status": snapshot.status, "redactions": snapshot.redactions}


def sanitize_model_request(
    messages: object,
    tools: object = (),
    parameters: Mapping[str, object] | None = None,
    model: str = "",
) -> dict[str, object]:
    snapshot = _Snapshot()
    result = snapshot.visit(
        {"model": model, "messages": messages, "tools": tools, "parameters": dict(parameters or {})}
    )
    return {**result, "capture_status": snapshot.status, "redactions": snapshot.redactions}


def sanitize_model_response(
    response: object = None, *, model: str = "", error: BaseException | None = None
) -> dict[str, object]:
    """Read only public answer/tool/usage fields, excluding provider reasoning."""
    choices = _field(response, "choices")
    choice = choices[0] if isinstance(choices, (list, tuple)) and choices else None
    message = _field(choice, "message")
    content = _field(message, "content") if message is not None else _field(response, "content")
    calls = _field(message, "tool_calls") if message is not None else _field(response, "tool_calls")
    candidates = _field(response, "candidates")
    candidate = candidates[0] if isinstance(candidates, list) and candidates else None
    if content is None and candidate is not None:
        content = _field(_field(candidate, "content"), "parts")
    safe_calls = []
    if isinstance(calls, (list, tuple)):
        for call in calls:
            function = _field(call, "function")
            safe_calls.append(
                {
                    "id": _field(call, "id"),
                    "type": _field(call, "type"),
                    "function": {"name": _field(function, "name"), "arguments": _field(function, "arguments")},
                }
            )
    if isinstance(content, list):
        for block in content:
            if isinstance(block, Mapping) and block.get("type") == "tool_use":
                safe_calls.append(
                    {
                        "id": block.get("id"),
                        "type": "function",
                        "function": {"name": block.get("name"), "arguments": block.get("input")},
                    }
                )
            native_call = block.get("functionCall") if isinstance(block, Mapping) else None
            if isinstance(native_call, Mapping):
                safe_calls.append(
                    {
                        "id": native_call.get("id"),
                        "type": "function",
                        "function": {"name": native_call.get("name"), "arguments": native_call.get("args")},
                    }
                )
    raw: dict[str, object] = {
        "model": _field(response, "model") or model,
        "content": content,
        "tool_calls": safe_calls,
        "finish_reason": _field(choice, "finish_reason")
        or _field(response, "stop_reason")
        or _field(candidate, "finishReason"),
        "usage": normalize_usage(_field(response, "usage") or _field(response, "usageMetadata")),
    }
    provider_error = _field(response, "error")
    if provider_error is not None:
        raw["error"] = provider_error
    refusal = _field(message, "refusal")
    if refusal is not None:
        raw["refusal"] = refusal
    # Mark omitted provider fields without ever traversing their private contents.
    snapshot = _Snapshot()
    if response is None and error is None:
        snapshot.omit("$", "response_body_not_recorded", response)
    if isinstance(choices, (list, tuple)) and len(choices) > 1:
        raw["choices"] = [
            {
                "index": _field(item, "index"),
                "content": _field(_field(item, "message"), "content"),
                "finish_reason": _field(item, "finish_reason"),
            }
            for item in choices
        ]
    for owner, prefix in ((response, "$"), (message, "$.message")):
        for name in ("reasoning_content", "reasoning_details", "thinking", "images", "audio"):
            value = _field(owner, name)
            if value is not None:
                snapshot.omit(f"{prefix}.{name}", "private_content", value)
    if error is not None:
        raw["error"] = {"type": type(error).__name__, "message": str(error)}
    safe = snapshot.visit(raw)
    return {**safe, "capture_status": snapshot.status, "redactions": snapshot.redactions}
