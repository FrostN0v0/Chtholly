"""Bounded input validation, never a native-code security boundary."""

from __future__ import annotations

from io import BytesIO
import re
import json
import hashlib
from pathlib import PurePosixPath
import tokenize
import unicodedata
from collections.abc import Mapping

from .codec import manifest_payload
from .models import Manifest, CommandCheck, WorkshopError

MAX_FILES = 32
MAX_FILE_BYTES = 64 * 1024
MAX_SOURCE_BYTES = 256 * 1024
MAX_MANIFEST_BYTES = 128 * 1024
_NAME = re.compile(r"[a-z][a-z0-9_]{2,39}\Z", re.ASCII)
_COMPONENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}\Z", re.ASCII)
_ALLOWED_EXTENSIONS = frozenset(
    {
        ".py",
        ".txt",
        ".json",
        ".html",
        ".css",
        ".js",
        ".svg",
        ".jinja",
        ".jinja2",
        ".j2",
        ".csv",
        ".toml",
        ".yaml",
        ".yml",
        ".md",
    }
)
_RESERVED = frozenset({"con", "prn", "aux", "nul", "clock$", "conin$", "conout$"})
_MANIFEST_KEYS = frozenset(
    {"title", "description", "commands", "permissions", "data_description", "configuration", "checks"}
)


def _text(value: object, label: str, maximum: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise WorkshopError(
            f"{label} must be {'possibly empty ' if empty else 'nonempty '}text of at most {maximum} characters"
        )
    if any(
        unicodedata.category(char) == "Cs" or (ord(char) < 32 and char not in "\n\r\t") or ord(char) == 127
        for char in value
    ):
        raise WorkshopError(f"{label} contains unsupported characters")
    return value


def canonical_json(value: object) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode(
            "utf-8"
        )
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise WorkshopError("Value must be finite, UTF-8 JSON") from exc


def _json_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise WorkshopError("configuration must be a JSON object")
    pending: list[tuple[object, int]] = [(value, 0)]
    inspected = 0
    while pending:
        item, depth = pending.pop()
        inspected += 1
        if depth > 16 or inspected > 4096:
            raise WorkshopError("configuration is too deeply nested or complex", code="limit_exceeded", status=413)
        if isinstance(item, dict):
            if any(not isinstance(key, str) or len(key) > 16 * 1024 for key in item):
                raise WorkshopError("configuration object keys must be bounded strings")
            if len(item) > 4096:
                raise WorkshopError("configuration contains too many values", code="limit_exceeded", status=413)
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            if len(item) > 4096:
                raise WorkshopError("configuration contains too many values", code="limit_exceeded", status=413)
            pending.extend((child, depth + 1) for child in item)
        elif item is not None and not isinstance(item, (str, bool, int, float)):
            raise WorkshopError("configuration must contain only JSON values")
        elif isinstance(item, str) and len(item) > 16 * 1024:
            raise WorkshopError("configuration text exceeds 16 KiB", code="limit_exceeded", status=413)
        elif isinstance(item, int) and item.bit_length() > 54000:
            raise WorkshopError("configuration number exceeds 16 KiB", code="limit_exceeded", status=413)
    if any(key.startswith("$") for key in value):
        raise WorkshopError("Top-level Entari $ control keys are not allowed in configuration")
    encoded = canonical_json(value)
    if len(encoded) > 16 * 1024:
        raise WorkshopError("configuration exceeds 16 KiB", code="limit_exceeded", status=413)
    return json.loads(encoded)


def parse_manifest(payload: Mapping[str, object]) -> Manifest:
    if not isinstance(payload, Mapping) or set(payload) - _MANIFEST_KEYS:
        raise WorkshopError("manifest must be an object containing only documented fields")
    title = _text(payload.get("title"), "title", 200)
    description = _text(payload.get("description"), "description", 4000)
    data_description = _text(payload.get("data_description"), "data_description", 4000)
    commands = payload.get("commands")
    if not isinstance(commands, list) or not 1 <= len(commands) <= 24:
        raise WorkshopError("commands must contain 1 to 24 bare command heads")
    for command in commands:
        _text(command, "command head", 100)
        if any(char.isspace() or ord(char) < 32 for char in command) or command.startswith(("/", "$")):
            raise WorkshopError("commands must be bare command heads without prefixes or whitespace")
    if len(set(commands)) != len(commands):
        raise WorkshopError("commands must be distinct")
    permissions = payload.get("permissions")
    if not isinstance(permissions, list) or len(permissions) > 32:
        raise WorkshopError("permissions must be a list of at most 32 declarations")
    for permission in permissions:
        _text(permission, "permission declaration", 500)
    raw_checks = payload.get("checks")
    if not isinstance(raw_checks, list) or not 1 <= len(raw_checks) <= 24:
        raise WorkshopError("checks must contain 1 to 24 acceptance checks")
    checks: list[CommandCheck] = []
    checked: set[str] = set()
    for item in raw_checks:
        if not isinstance(item, Mapping) or set(item) - {"command", "expected_contains", "operator", "repeatable"}:
            raise WorkshopError("Invalid acceptance check fields")
        command = _text(item.get("command"), "check command", 500)
        expected = _text(item.get("expected_contains"), "expected_contains", 2000)
        operator = item.get("operator", False)
        repeatable = item.get("repeatable", True)
        if type(operator) is not bool or type(repeatable) is not bool:
            raise WorkshopError("check operator and repeatable must be booleans")
        head = command.split()[0]
        if head not in commands:
            raise WorkshopError("Every check must invoke a declared command head")
        checked.add(head)
        checks.append(CommandCheck(command, expected, operator, repeatable))
    if checked != set(commands) or not any(check.repeatable for check in checks):
        raise WorkshopError("Every command needs a check and at least one check must be repeatable")
    manifest = Manifest(
        title,
        description,
        tuple(commands),
        tuple(permissions),
        data_description,
        _json_object(payload.get("configuration", {})),
        tuple(checks),
    )
    if len(canonical_json(manifest_payload(manifest))) > MAX_MANIFEST_BYTES:
        raise WorkshopError("manifest exceeds 128 KiB", code="limit_exceeded", status=413)
    return manifest


def module_name(name: str) -> str:
    if not isinstance(name, str) or _NAME.fullmatch(name) is None:
        raise WorkshopError("plugin name must match [a-z][a-z0-9_]{2,39}")
    return f"workshop_{name}"


def validate_path(path: object) -> str:
    if not isinstance(path, str) or len(path) > 240 or "\\" in path:
        raise WorkshopError("Source paths must be bounded relative POSIX paths", code="invalid_path")
    parts = path.split("/")
    if not 1 <= len(parts) <= 8:
        raise WorkshopError("Source package nesting exceeds 8 components", code="invalid_path")
    for part in parts:
        stem = part.split(".")[0].casefold()
        if (
            not _COMPONENT.fullmatch(part)
            or part.endswith((".", " "))
            or stem in _RESERVED
            or re.fullmatch(r"(?:com|lpt)[0-9]", stem)
            or part.casefold() == "__pycache__"
        ):
            raise WorkshopError("Source path contains an unsafe or Windows-reserved component", code="invalid_path")
    if PurePosixPath(path).suffix.casefold() not in _ALLOWED_EXTENSIONS:
        raise WorkshopError("Source file extension is not allowed", code="invalid_path")
    if PurePosixPath(path).suffix.casefold() == ".py":
        if not all(part.isidentifier() for part in (*parts[:-1], PurePosixPath(path).stem)):
            raise WorkshopError("Python module paths must use ordinary identifiers", code="invalid_path")
        if not path.endswith(".py"):
            raise WorkshopError("Python sources must use lowercase .py", code="invalid_path")
    return path


def normalize_submission(name: str, files: Mapping[str, str], manifest: Manifest) -> tuple[str, dict[str, str], str]:
    module_name(name)
    if not isinstance(manifest, Manifest):
        raise WorkshopError("manifest must be a Manifest")
    checked_manifest = parse_manifest(manifest_payload(manifest))
    if not isinstance(files, Mapping) or not 1 <= len(files) <= MAX_FILES:
        raise WorkshopError("Submission must contain 1 to 32 files", code="limit_exceeded", status=413)
    result: dict[str, str] = {}
    nodes: dict[str, tuple[str, bool]] = {}
    total = 0
    for path, content in files.items():
        path = validate_path(path)
        if not isinstance(content, str):
            raise WorkshopError("Source contents must be UTF-8 text")
        if len(content) > MAX_FILE_BYTES:
            raise WorkshopError("Source file exceeds 64 KiB", code="limit_exceeded", status=413)
        try:
            raw = content.encode("utf-8")
        except UnicodeError as exc:
            raise WorkshopError("Source contents must be valid UTF-8 text") from exc
        total += len(raw)
        if len(raw) > MAX_FILE_BYTES or total > MAX_SOURCE_BYTES:
            raise WorkshopError("Submission exceeds per-file or total source limit", code="limit_exceeded", status=413)
        parts = path.split("/")
        for index in range(1, len(parts) + 1):
            node = "/".join(parts[:index])
            kind = index == len(parts)
            previous = nodes.get(node.casefold())
            if previous is not None and (previous != (node, kind) or kind):
                raise WorkshopError("Source paths have a case or file/directory collision", code="invalid_path")
            nodes[node.casefold()] = (node, kind)
        if path.endswith(".py"):
            try:
                encoding, _ = tokenize.detect_encoding(BytesIO(raw).readline)
            except SyntaxError as exc:
                raise WorkshopError(f"Invalid Python encoding declaration in {path}", code="invalid_source") from exc
            if encoding not in {"utf-8", "utf-8-sig"}:
                raise WorkshopError("Python sources must declare UTF-8 encoding", code="invalid_source")
            try:
                compile(raw, path, "exec", dont_inherit=True)
            except (SyntaxError, ValueError, RecursionError) as exc:
                line = getattr(exc, "lineno", None)
                raise WorkshopError(
                    f"Python syntax is invalid in {path}" + (f" at line {line}" if line else ""), code="invalid_source"
                ) from exc
        result[path] = content
    if "__init__.py" not in result:
        raise WorkshopError("Submission requires __init__.py", code="invalid_source")
    digest = hashlib.sha256(b"chtholly-plugin-workshop-v1\0")
    manifest_bytes = canonical_json(manifest_payload(checked_manifest))
    digest.update(len(manifest_bytes).to_bytes(8, "big"))
    digest.update(manifest_bytes)
    for path in sorted(result):
        path_bytes = path.encode("utf-8")
        raw = result[path].encode("utf-8")
        digest.update(len(path_bytes).to_bytes(4, "big"))
        digest.update(path_bytes)
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
    return name, result, digest.hexdigest()
