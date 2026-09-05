"""Pure validation helpers for web-artifact inputs and manifests."""

from __future__ import annotations

import re
import math
import base64
from typing import TYPE_CHECKING
import hashlib
import binascii
from dataclasses import dataclass
import unicodedata
from collections.abc import Mapping, Sequence

from .errors import ArtifactError, ArtifactLimitError
from .models import ArtifactFileInfo

if TYPE_CHECKING:
    from .models import ArtifactLimits


_ALLOWED_MIME_BY_SUFFIX: dict[str, str] = {
    ".html": "text/html",
    ".css": "text/css",
    ".js": "text/javascript",
    ".json": "application/json",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
_BINARY_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
_RESERVED_DEVICE_NAMES = frozenset({"con", "prn", "aux", "nul", "clock$"})
_HEX_ESCAPE = re.compile(r"%[0-9a-fA-F]{2}")
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
_CONTROL_CHARS = frozenset(chr(index) for index in range(32)) | {chr(127)}


class ValidationError(ArtifactError):
    """Raised for malformed artifact input before it reaches storage."""


@dataclass(frozen=True, slots=True)
class ValidatedFile:
    """Decoded, validated source file ready for immutable publication."""

    path: str
    data: bytes
    mime: str
    encoding: str
    sha256: str

    @property
    def info(self) -> ArtifactFileInfo:
        return ArtifactFileInfo(
            path=self.path,
            mime=self.mime,
            size=len(self.data),
            sha256=self.sha256,
            encoding=self.encoding,
        )


def normalize_artifact_path(path: str) -> str:
    """Return a safe canonical relative path or raise ``ValidationError``.

    Artifact paths are logical POSIX paths even on Windows.  Percent-encoded
    bytes are rejected rather than decoded so a URL router cannot create a
    second spelling for a registered file.
    """

    if not isinstance(path, str) or not path:
        raise ValidationError("file path must be a non-empty string")
    try:
        encoded_path = path.encode("utf-8")
    except UnicodeError as exc:
        raise ValidationError("file path must be valid UTF-8") from exc
    if len(path) > 240 or len(encoded_path) > 1024:
        raise ValidationError("file path is too long")
    if any(char in _CONTROL_CHARS for char in path):
        raise ValidationError("file path contains a control character")
    if _DRIVE_PREFIX.match(path) or path.startswith(("/", "\\")) or "\\" in path:
        raise ValidationError("file path must be a relative POSIX path")
    if any(_HEX_ESCAPE.match(path[index : index + 3]) for index in range(len(path) - 2)):
        raise ValidationError("percent-encoded path bytes are not allowed")

    normalized = unicodedata.normalize("NFC", path)
    segments = normalized.split("/")
    if not segments or any(not segment for segment in segments):
        raise ValidationError("file path contains an empty segment")
    for segment in segments:
        if segment in {".", ".."}:
            raise ValidationError("file path traversal is not allowed")
        if segment.rstrip(" .") != segment:
            raise ValidationError("file path may not end a segment with dot or space")
        if any(char in '<>:"|?*' for char in segment):
            raise ValidationError("file path contains a forbidden character")
        device = segment.split(".", 1)[0].casefold()
        if device in _RESERVED_DEVICE_NAMES or re.fullmatch(r"(?:com|lpt)[0-9]+", device):
            raise ValidationError("file path uses a reserved device name")
    return normalized


def path_casefold(path: str) -> str:
    """Return the filesystem-independent collision key for a path."""

    return unicodedata.normalize("NFC", path).casefold()


def mime_for_artifact_path(path: str) -> str:
    """Return the only MIME permitted for a registered artifact suffix."""

    normalized = normalize_artifact_path(path)
    basename = normalized.rsplit("/", 1)[-1]
    suffix = "." + basename.rsplit(".", 1)[-1].casefold() if "." in basename else ""
    mime = _ALLOWED_MIME_BY_SUFFIX.get(suffix)
    if mime is None:
        raise ValidationError("file type is not allowed")
    return mime


def validate_entry(entry: str) -> str:
    """Validate the project's HTML entry path."""

    normalized = normalize_artifact_path(entry)
    if not normalized.casefold().endswith(".html"):
        raise ValidationError("entry must be an .html file")
    return normalized


def validate_title(title: str) -> str:
    """Validate and trim a human-facing title without allowing controls."""

    if not isinstance(title, str):
        raise ValidationError("title must be a string")
    value = title.strip()
    if not value or len(value) > 200:
        raise ValidationError("title must contain 1 to 200 characters")
    if any(char in _CONTROL_CHARS for char in value):
        raise ValidationError("title contains a control character")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise ValidationError("title must be valid UTF-8") from exc
    return value


def validate_turn_key(turn_key: str) -> str:
    """Validate the persistent per-turn quota key."""

    if not isinstance(turn_key, str):
        raise ValidationError("turn_key must be a string")
    if len(turn_key) > 256 or any(char in _CONTROL_CHARS for char in turn_key):
        raise ValidationError("turn_key is invalid")
    try:
        turn_key.encode("utf-8")
    except UnicodeError as exc:
        raise ValidationError("turn_key must be valid UTF-8") from exc
    return turn_key


def validate_ttl_hours(ttl_hours: float, max_ttl_hours: int) -> float:
    """Validate a positive finite TTL against the store's configured bound."""

    if isinstance(ttl_hours, bool) or not isinstance(ttl_hours, (int, float)):
        raise ValidationError("ttl_hours must be a number")
    value = float(ttl_hours)
    if not math.isfinite(value) or value <= 0 or value > max_ttl_hours:
        raise ValidationError(f"ttl_hours must be in the range (0, {max_ttl_hours}]")
    return value


def sniff_image_mime(data: bytes) -> str | None:
    """Return the MIME detected from a supported image signature."""

    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def is_png(data: bytes) -> bool:
    """Return whether bytes begin with the PNG signature."""

    return data.startswith(b"\x89PNG\r\n\x1a\n")


def _decode_content(content: object, encoding: object, max_decoded_bytes: int) -> tuple[bytes, str]:
    if not isinstance(content, str):
        raise ValidationError("file content must be a string")
    if encoding is None:
        normalized_encoding = "utf-8"
    elif isinstance(encoding, str):
        normalized_encoding = encoding.casefold()
    else:
        raise ValidationError("file encoding must be utf-8 or base64")

    if normalized_encoding in {"utf8", "utf-8"}:
        if len(content) > max_decoded_bytes:
            raise ValidationError("file content is too large")
        try:
            data = content.encode("utf-8")
        except UnicodeError as exc:
            raise ValidationError("file content is not valid UTF-8") from exc
        if len(data) > max_decoded_bytes:
            raise ValidationError("file content is too large")
        return data, "utf-8"
    if normalized_encoding == "base64":
        if len(content) > ((max_decoded_bytes + 2) // 3) * 4:
            raise ValidationError("base64 file content is too large")
        try:
            encoded = content.encode("ascii")
            data = base64.b64decode(encoded, validate=True)
        except (UnicodeError, ValueError, binascii.Error) as exc:
            raise ValidationError("file content is not valid base64") from exc
        if len(data) > max_decoded_bytes:
            raise ValidationError("file content is too large")
        return data, "base64"
    raise ValidationError("file encoding must be utf-8 or base64")


def validate_file_mapping(file_mapping: Mapping[str, object], limits: ArtifactLimits) -> ValidatedFile:
    """Decode and validate one public ``path/content/encoding`` mapping."""

    if not isinstance(file_mapping, Mapping):
        raise ValidationError("each file must be a mapping")
    raw_path = file_mapping.get("path")
    if not isinstance(raw_path, str):
        raise ValidationError("file path must be a string")
    path = normalize_artifact_path(raw_path)
    suffix = "." + path.rsplit("/", 1)[-1].rsplit(".", 1)[-1].casefold() if "." in path.rsplit("/", 1)[-1] else ""
    mime = _ALLOWED_MIME_BY_SUFFIX.get(suffix)
    if mime is None:
        raise ValidationError("file type is not allowed")
    data, encoding = _decode_content(file_mapping.get("content"), file_mapping.get("encoding"), limits.max_file_bytes)
    if suffix in _BINARY_SUFFIXES:
        if encoding != "base64":
            raise ValidationError("binary image files must use base64 encoding")
        detected = sniff_image_mime(data)
        if detected != mime:
            raise ValidationError("image bytes do not match the file suffix")
    else:
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationError("text files must contain valid UTF-8") from exc
    digest = hashlib.sha256(data).hexdigest()
    return ValidatedFile(path=path, data=data, mime=mime, encoding=encoding, sha256=digest)


def validate_input_files(files: Sequence[Mapping[str, object]], limits: ArtifactLimits) -> tuple[ValidatedFile, ...]:
    """Validate all submitted files and reject exact/casefold collisions."""

    if isinstance(files, (str, bytes, bytearray)):
        raise ValidationError("files must be a sequence of mappings")
    try:
        file_count = len(files)
    except TypeError as exc:
        raise ValidationError("files must be a sequence of mappings") from exc
    if file_count > limits.max_files:
        raise ArtifactLimitError("project exceeds max_files")

    result: list[ValidatedFile] = []
    seen: set[str] = set()
    total_bytes = 0
    for file_mapping in files:
        item = validate_file_mapping(file_mapping, limits)
        key = path_casefold(item.path)
        if key in seen:
            raise ValidationError("file paths collide case-insensitively")
        seen.add(key)
        result.append(item)
        total_bytes += len(item.data)
        if total_bytes > limits.max_project_bytes:
            raise ArtifactLimitError("project exceeds max_project_bytes")
    return tuple(sorted(result, key=lambda item: item.path))


def validate_delete_paths(delete_paths: Sequence[str]) -> tuple[str, ...]:
    """Validate explicit inherited-file deletion paths."""

    if isinstance(delete_paths, (str, bytes, bytearray)):
        raise ValidationError("delete_paths must be a sequence")
    try:
        iterator = iter(delete_paths)
    except TypeError as exc:
        raise ValidationError("delete_paths must be a sequence") from exc
    result: list[str] = []
    seen: set[str] = set()
    for path in iterator:
        normalized = normalize_artifact_path(path)
        key = path_casefold(normalized)
        if key in seen:
            raise ValidationError("delete_paths contain a casefold collision")
        seen.add(key)
        result.append(normalized)
    return tuple(result)


def source_digest(files: Sequence[ValidatedFile]) -> str:
    """Hash canonical path, length, and content tuples for a project."""

    digest = hashlib.sha256()
    for item in sorted(files, key=lambda value: value.path):
        path_bytes = item.path.encode("utf-8")
        digest.update(len(path_bytes).to_bytes(4, "big"))
        digest.update(path_bytes)
        digest.update(len(item.data).to_bytes(8, "big"))
        digest.update(item.data)
    return digest.hexdigest()
