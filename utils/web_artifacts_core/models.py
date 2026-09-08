"""Immutable models and limits for versioned web artifacts."""

from __future__ import annotations

import math
from typing import Final
from dataclasses import dataclass

# These ceilings are intentionally part of the model rather than configurable
# through an environment variable.  A deployment may tighten a limit, but it
# cannot use the store as an unbounded file host by supplying a larger value.
_HARD_MAX_FILES: Final = 32
_HARD_MAX_FILE_BYTES: Final = 6 * 1024 * 1024
_HARD_MAX_PROJECT_BYTES: Final = 8 * 1024 * 1024
_HARD_MAX_ZIP_BYTES: Final = 10 * 1024 * 1024
_HARD_MAX_PREVIEW_BYTES: Final = 3 * 1024 * 1024
_HARD_MAX_ACTIVE_PER_OWNER: Final = 10
_HARD_MAX_ACTIVE_GLOBAL: Final = 200
_HARD_MAX_TOTAL_BYTES: Final = 2 * 1024**3
_HARD_MAX_PER_TURN: Final = 3
_HARD_MAX_TTL_HOURS: Final = 168


@dataclass(frozen=True, slots=True)
class ArtifactOwner:
    """The immutable ownership principal used by the store."""

    scope_id: int
    user_id: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.scope_id, bool)
            or not isinstance(self.scope_id, int)
            or self.scope_id < 0
            or self.scope_id > 2**63 - 1
        ):
            raise ValueError("scope_id must be an integer in SQLite's non-negative range")
        if not isinstance(self.user_id, str) or not self.user_id or len(self.user_id) > 256:
            raise ValueError("user_id must be a non-empty string of at most 256 characters")
        if "\x00" in self.user_id:
            raise ValueError("user_id must not contain NUL")
        try:
            self.user_id.encode("utf-8")
        except UnicodeError as exc:
            raise ValueError("user_id must be valid UTF-8") from exc


@dataclass(frozen=True, slots=True)
class ArtifactLimits:
    """Hard-bounded quotas for one :class:`ArtifactStore` instance."""

    max_files: int = 32
    max_file_bytes: int = 6 * 1024 * 1024
    max_project_bytes: int = 8 * 1024 * 1024
    max_zip_bytes: int = 10 * 1024 * 1024
    max_preview_bytes: int = 3 * 1024 * 1024
    max_active_per_owner: int = 10
    max_active_global: int = 200
    max_total_bytes: int = 2 * 1024**3
    max_per_turn: int = 3
    max_ttl_hours: int = 168

    def __post_init__(self) -> None:
        bounds = {
            "max_files": (self.max_files, 1, _HARD_MAX_FILES),
            "max_file_bytes": (self.max_file_bytes, 1, _HARD_MAX_FILE_BYTES),
            "max_project_bytes": (self.max_project_bytes, 1, _HARD_MAX_PROJECT_BYTES),
            "max_zip_bytes": (self.max_zip_bytes, 1, _HARD_MAX_ZIP_BYTES),
            "max_preview_bytes": (self.max_preview_bytes, 1, _HARD_MAX_PREVIEW_BYTES),
            "max_active_per_owner": (self.max_active_per_owner, 1, _HARD_MAX_ACTIVE_PER_OWNER),
            "max_active_global": (self.max_active_global, 1, _HARD_MAX_ACTIVE_GLOBAL),
            "max_total_bytes": (self.max_total_bytes, 1, _HARD_MAX_TOTAL_BYTES),
            "max_per_turn": (self.max_per_turn, 1, _HARD_MAX_PER_TURN),
            "max_ttl_hours": (self.max_ttl_hours, 1, _HARD_MAX_TTL_HOURS),
        }
        for name, (value, lower, upper) in bounds.items():
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{name} must be an integer")
            if value < lower or value > upper:
                raise ValueError(f"{name} must be between {lower} and {upper}")


@dataclass(frozen=True, slots=True)
class ArtifactFileInfo:
    """Manifest information for one immutable source file."""

    path: str
    mime: str
    size: int
    sha256: str
    encoding: str


@dataclass(frozen=True, slots=True)
class Artifact:
    """Public metadata for one immutable artifact version."""

    artifact_ref: str
    project_ref: str
    version: int
    title: str
    entry: str
    created_at: float
    expires_at: float
    token: str
    files: tuple[ArtifactFileInfo, ...]
    source_bytes: int
    zip_bytes: int
    source_sha256: str

    def __post_init__(self) -> None:
        for name in ("created_at", "expires_at"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
                raise ValueError(f"{name} must be a finite number")
        if self.expires_at < self.created_at:
            raise ValueError("expires_at must not precede created_at")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise ValueError("version must be a positive integer")
        if isinstance(self.source_bytes, bool) or not isinstance(self.source_bytes, int) or self.source_bytes < 0:
            raise ValueError("source_bytes must be a non-negative integer")
        if isinstance(self.zip_bytes, bool) or not isinstance(self.zip_bytes, int) or self.zip_bytes < 0:
            raise ValueError("zip_bytes must be a non-negative integer")
        if not isinstance(self.files, tuple):
            raise ValueError("files must be a tuple")
