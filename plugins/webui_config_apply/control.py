"""Unprivileged access to the root-owned config-apply control files."""

from __future__ import annotations

import os
import re
import json
import stat
from uuid import uuid4
from typing import Any
from hashlib import sha256
from pathlib import Path
from datetime import datetime
import subprocess

from arclet.entari.plugin import get_plugins

from .saving import SaveError

_CONTROL_DIR = Path("/run/chtholly-config-apply")
_STATUS_PATH = _CONTROL_DIR / "status.json"
_REQUEST_PATH = _CONTROL_DIR / "restart.request"
_RESULTS = frozenset(
    {"checking", "restarting", "verifying", "applied", "unchanged", "rejected", "rolled_back", "rollback_failed"}
)
_HEX64 = re.compile(r"[0-9a-f]{64}")
_HEX32 = re.compile(r"[0-9a-f]{32}")


def file_digest(path: Path) -> str | None:
    try:
        return sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _trusted(info: os.stat_result, *, directory: bool = False, writable: bool = False) -> bool:
    if os.name != "posix" or info.st_uid != 0:
        return False
    valid_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    unsafe_mode = 0o002 if writable else 0o022
    return valid_type and not info.st_mode & unsafe_mode


def _request_available() -> bool:
    try:
        return (
            _trusted(_CONTROL_DIR.lstat(), directory=True)
            and _trusted(_REQUEST_PATH.lstat(), writable=True)
            and os.access(_REQUEST_PATH, os.W_OK)
            and subprocess.run(
                ["/usr/bin/systemctl", "is-active", "--quiet", "chtholly-config-apply.path"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=False,
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


def _safe_status(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("result") not in _RESULTS:
        raise ValueError("Invalid apply status")
    updated = raw.get("updated_at")
    if not isinstance(updated, str) or len(updated) > 40:
        raise ValueError("Invalid apply status")
    datetime.fromisoformat(updated.replace("Z", "+00:00"))
    state: dict[str, Any] = {"result": raw["result"], "updated_at": updated}
    digest = raw.get("candidate_sha256")
    if isinstance(digest, str) and _HEX64.fullmatch(digest):
        state["candidate_sha256"] = digest
    request_id = raw.get("request_id")
    if isinstance(request_id, str) and _HEX32.fullmatch(request_id):
        state["request_id"] = request_id
    reason = raw.get("reason")
    if isinstance(reason, str):
        state["reason"] = "".join(char for char in reason[:500] if char.isprintable())
    attempt = raw.get("restart_attempt")
    if isinstance(attempt, int) and not isinstance(attempt, bool) and 0 <= attempt <= 100:
        state["restart_attempt"] = attempt
    return state


def read_status() -> tuple[bool, dict[str, Any]]:
    try:
        if not _trusted(_CONTROL_DIR.lstat(), directory=True):
            return False, {}
        descriptor = os.open(_STATUS_PATH, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            if not _trusted(os.fstat(stream.fileno())):
                return False, {}
            content = stream.read(16385)
        if len(content) > 16384:
            return False, {}
        state = _safe_status(json.loads(content))
        return _request_available(), state
    except (OSError, ValueError, TypeError):
        return False, {}


def status_payload(config_path: Path, running_sha256: str | None) -> dict[str, Any]:
    available, state = read_status()
    saved = file_digest(config_path)
    return {
        "success": True,
        "available": available,
        "state": state,
        "running_sha256": running_sha256,
        "saved_sha256": saved,
        "in_sync": running_sha256 is not None and saved == running_sha256,
        "loaded_config_keys": sorted({plug._config_key for plug in get_plugins()}),
    }


def request_restart() -> str:
    available, _ = read_status()
    if not available:
        raise SaveError("The managed restart helper is unavailable", code="helper_unavailable", status=503)
    request_id = uuid4().hex
    try:
        # No create/truncate flags: validate the precreated inode before writing.
        descriptor = os.open(_REQUEST_PATH, os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "wb") as stream:
            if not _trusted(os.fstat(stream.fileno()), writable=True):
                raise SaveError("The managed restart helper is unavailable", code="helper_unavailable", status=503)
            stream.write(request_id.encode("ascii"))
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        raise SaveError(
            "The managed restart request could not be written", code="restart_request_failed", status=503
        ) from None
    return request_id
