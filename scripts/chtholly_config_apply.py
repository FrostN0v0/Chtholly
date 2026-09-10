#!/opt/chtholly/.venv/bin/python
"""Apply saved configuration with health-checked hot updates or process restarts."""

from __future__ import annotations

import os
import re
import grp
import json
import time
from uuid import uuid4
import fcntl
import hashlib
import logging
from pathlib import Path
from datetime import datetime, timezone
import subprocess
from http.cookies import SimpleCookie
import urllib.error
import urllib.request
from collections.abc import Mapping

from utils.webui_config_core import validate_candidate
from utils.webui_config_core.templates import expand_environment_template

CONFIG_PATH = Path("/opt/chtholly/entari.yml")
STATE_DIR = Path("/var/lib/chtholly-config-apply")
RUNTIME_DIR = Path("/run/chtholly-config-apply")
LAST_GOOD_PATH = STATE_DIR / "last-good.yml"
STATUS_PATH = STATE_DIR / "status.json"
NEXT_PATH = STATE_DIR / "next.yml"
PUBLIC_STATUS_PATH = RUNTIME_DIR / "status.json"
REQUEST_PATH = RUNTIME_DIR / "restart.request"
LOCK_PATH = RUNTIME_DIR / "apply.lock"
SERVICE_NAME = "chtholly.service"
API_URL = "http://127.0.0.1:8120"
LOGGER = logging.getLogger("chtholly-config-apply")
_WEBUI_SESSION = ""


class _NoApiRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.URLError("Controller API redirects are not allowed")


_API_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoApiRedirects())


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write(path: Path, data: bytes, *, mode: int = 0o600, gid: int = 0) -> None:
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as stream:
            os.fchmod(stream.fileno(), mode)
            os.fchown(stream.fileno(), 0, gid)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def write_config(data: bytes) -> None:
    # The service has a writable file mount, not a writable source directory.
    with CONFIG_PATH.open("r+b") as stream:
        stream.write(data)
        stream.truncate()
        stream.flush()
        os.fsync(stream.fileno())


def record_status(
    result: str,
    candidate: bytes,
    request_id: str | None,
    *,
    reason: str = "",
    application_mode: str = "restart",
) -> None:
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "result": result,
        "candidate_sha256": digest(candidate),
        "request_id": request_id,
        "reason": reason,
        "application_mode": application_mode,
    }
    data = json.dumps(payload, separators=(",", ":")).encode()
    atomic_write(STATUS_PATH, data)
    atomic_write(PUBLIC_STATUS_PATH, data, mode=0o640, gid=grp.getgrnam("chtholly").gr_gid)


def read_stable_candidate() -> bytes:
    time.sleep(2)
    previous = CONFIG_PATH.read_bytes()
    for _ in range(8):
        time.sleep(0.5)
        current = CONFIG_PATH.read_bytes()
        if current == previous:
            return current
        previous = current
    raise RuntimeError("configuration did not become stable")


def take_restart_request() -> str | None:
    # The root-owned directory cannot be renamed or populated by the Bot.
    with REQUEST_PATH.open("r+b") as stream:
        request = stream.read(65)
        if not request:
            return None
        stream.seek(0)
        stream.truncate()
        stream.flush()
        os.fsync(stream.fileno())
    if re.fullmatch(rb"[0-9a-f]{32}", request):
        return request.decode("ascii")
    return None


def run_systemctl(*arguments: str) -> bool:
    try:
        result = subprocess.run(
            ["/usr/bin/systemctl", *arguments, SERVICE_NAME],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=45,
        )
    except subprocess.TimeoutExpired:
        LOGGER.error("Service operation timed out: %s", arguments[0])
        return False
    return result.returncode == 0


def _authenticate_webui() -> str:
    config = plugin_config(validate_candidate(CONFIG_PATH.read_bytes(), os.environ))
    webui = config.get("entari_plugin_webui", config.get("webui"))
    reference = webui.get("password") if isinstance(webui, Mapping) else None
    password = expand_environment_template(reference, os.environ) if isinstance(reference, str) else None
    if not password:
        raise ValueError("Controller requires the configured WebUI password environment reference")
    request = urllib.request.Request(
        API_URL + "/api/auth/login",
        data=json.dumps({"password": password}).encode(),
        headers={"Content-Type": "application/json", "Origin": API_URL},
        method="POST",
    )
    with _API_OPENER.open(request, timeout=3) as response:
        result = json.load(response)
        cookie = SimpleCookie()
        for header in response.headers.get_all("Set-Cookie", []):
            cookie.load(header)
    session = cookie.get("webui_sid")
    if not isinstance(result, dict) or result.get("success") is not True or session is None:
        raise ValueError("WebUI authentication did not establish a session")
    if re.fullmatch(r"[A-Za-z0-9_-]{16,128}", session.value) is None:
        raise ValueError("WebUI returned an invalid session identifier")
    return session.value


def request_json(path: str) -> Mapping[str, object]:
    global _WEBUI_SESSION

    def fetch():
        # The fixed loopback API uses Secure cookies; non-browser cookie jars omit them on HTTP.
        headers = {"Cookie": f"webui_sid={_WEBUI_SESSION}"} if _WEBUI_SESSION else {}
        request = urllib.request.Request(API_URL + path, headers=headers)
        with _API_OPENER.open(request, timeout=2) as response:
            return json.load(response)

    try:
        result = fetch()
    except urllib.error.HTTPError as error:
        if error.code != 401:
            raise
        _WEBUI_SESSION = _authenticate_webui()
        result = fetch()
    if not isinstance(result, dict):
        raise ValueError("API response must be an object")
    return result


def health_ready(expected: str, required_plugins: set[str], *, require_synced: bool = False) -> bool:
    try:
        if not run_systemctl("is-active", "--quiet"):
            return False
        if request_json("/api/health").get("status") != "ok":
            return False
        status = request_json("/api/config-apply/status")
        keys = status.get("loaded_config_keys")
        if require_synced and (
            status.get("success") is not True
            or status.get("saved_sha256") != expected
            or status.get("in_sync") is not True
        ):
            return False
        return (
            status.get("running_sha256") == expected
            and isinstance(keys, list)
            and all(isinstance(key, str) for key in keys)
            and required_plugins.issubset(keys)
        )
    except (OSError, ValueError, urllib.error.URLError):
        return False


def wait_for_health(expected: str, required_plugins: set[str], timeout: float = 90) -> bool:
    deadline = time.monotonic() + timeout
    consecutive = 0
    while time.monotonic() < deadline:
        if digest(CONFIG_PATH.read_bytes()) != expected:
            return False
        consecutive = consecutive + 1 if health_ready(expected, required_plugins) else 0
        if consecutive >= 5:
            return True
        time.sleep(1)
    return False


def already_applied(candidate: bytes, required_plugins: set[str]) -> bool:
    expected = digest(candidate)
    # Do not wait out the startup timeout when a normal restart is needed.
    for check in range(5):
        if CONFIG_PATH.read_bytes() != candidate or not health_ready(expected, required_plugins, require_synced=True):
            return False
        if check < 4:
            time.sleep(1)
    return CONFIG_PATH.read_bytes() == candidate


def plugin_config(data: Mapping[str, object]) -> Mapping[str, object]:
    root = data.get("entari", data)
    if not isinstance(root, Mapping):
        return {}
    plugins = root.get("plugins")
    return plugins if isinstance(plugins, Mapping) else {}


def required_plugins(candidate: bytes, last_good: bytes) -> set[str]:
    current = plugin_config(validate_candidate(candidate, os.environ))
    previous = plugin_config(validate_candidate(last_good, os.environ))
    required = {"llm", "webui_config_apply"}
    for key, value in current.items():
        if key.startswith(("$", "~", "?")) or previous.get(key) == value:
            continue
        if isinstance(value, Mapping) and (value.get("$disable") or value.get("$optional")):
            continue
        required.add(key)
    return required


def save_failed_candidate(candidate: bytes) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = STATE_DIR / f"failed-{stamp}-{digest(candidate)[:12]}.yml"
    atomic_write(path, candidate)
    failures = sorted(STATE_DIR.glob("failed-*.yml"), key=lambda item: item.stat().st_mtime_ns, reverse=True)
    for stale in failures[5:]:
        stale.unlink(missing_ok=True)


def preserve_newer_candidate(failed: bytes, last_good: bytes) -> None:
    current = CONFIG_PATH.read_bytes()
    if current not in (failed, last_good):
        atomic_write(NEXT_PATH, current)


def restore_last_good(
    failed: bytes,
    last_good: bytes,
    request_id: str | None,
    *,
    reason: str,
    rejected: bool,
) -> bool:
    save_failed_candidate(failed)
    preserve_newer_candidate(failed, last_good)
    record_status("restarting", failed, request_id, reason=reason)
    if not run_systemctl("stop"):
        record_status("rollback_failed", failed, request_id, reason="Bot could not be stopped for recovery")
        return False
    preserve_newer_candidate(failed, last_good)
    write_config(last_good)
    record_status("verifying", failed, request_id, reason=reason)
    started = run_systemctl("start")
    recovered = started and wait_for_health(digest(last_good), {"llm", "webui_config_apply"})
    if started and CONFIG_PATH.read_bytes() != last_good:
        return True
    if not recovered:
        record_status("rollback_failed", failed, request_id, reason="Previous configuration did not recover")
        return False
    record_status("rejected" if rejected else "rolled_back", failed, request_id, reason=reason)
    LOGGER.warning(
        "Saved configuration was %s; previous configuration restored", "rejected" if rejected else "rolled back"
    )
    return True


def apply_candidate(candidate: bytes, last_good: bytes, request_id: str | None, *, staged: bool) -> bool:
    initial_disk = CONFIG_PATH.read_bytes()
    if staged and initial_disk != last_good:
        # A newer live save takes precedence over a recovery-queued candidate.
        candidate = initial_disk
    if (
        candidate == last_good
        and not request_id
        and health_ready(digest(candidate), {"llm", "webui_config_apply"}, require_synced=True)
    ):
        NEXT_PATH.unlink(missing_ok=True)
        # Do not erase the previous failure message on our own rollback trigger.
        return True
    record_status("checking", candidate, request_id)
    try:
        required = required_plugins(candidate, last_good)
    except ValueError as exc:
        NEXT_PATH.unlink(missing_ok=True)
        return restore_last_good(candidate, last_good, request_id, reason=str(exc), rejected=True)
    if not request_id and already_applied(candidate, required):
        atomic_write(LAST_GOOD_PATH, candidate)
        NEXT_PATH.unlink(missing_ok=True)
        (STATE_DIR / "pending.yml").unlink(missing_ok=True)
        record_status("applied", candidate, request_id, application_mode="hot_reload")
        LOGGER.info("Hot-applied configuration verified at %s", digest(candidate)[:12])
        return True
    record_status("restarting", candidate, request_id)
    if not run_systemctl("stop"):
        record_status("rollback_failed", candidate, request_id, reason="Bot could not be stopped")
        return False
    latest = CONFIG_PATH.read_bytes()
    if latest != initial_disk:
        candidate = latest
        try:
            required = required_plugins(candidate, last_good)
        except ValueError as exc:
            NEXT_PATH.unlink(missing_ok=True)
            return restore_last_good(candidate, last_good, request_id, reason=str(exc), rejected=True)
    if CONFIG_PATH.read_bytes() != candidate:
        write_config(candidate)
    NEXT_PATH.unlink(missing_ok=True)
    atomic_write(STATE_DIR / "pending.yml", candidate)
    record_status("verifying", candidate, request_id)
    started = run_systemctl("start")
    verified = started and wait_for_health(digest(candidate), required)
    if started and CONFIG_PATH.read_bytes() != candidate:
        # A newer hot save may have changed the runtime digest during verification.
        # Leave last-good intact and let main verify that new candidate.
        return True
    if not verified:
        return restore_last_good(
            candidate,
            last_good,
            request_id,
            reason="Saved configuration failed runtime or plugin readiness verification",
            rejected=False,
        )
    atomic_write(LAST_GOOD_PATH, candidate)
    (STATE_DIR / "pending.yml").unlink(missing_ok=True)
    record_status("applied", candidate, request_id)
    LOGGER.info("Configuration applied at %s", digest(candidate)[:12])
    return True


def main() -> int:
    if not LAST_GOOD_PATH.is_file():
        raise RuntimeError("last-good baseline is unavailable")
    with LOCK_PATH.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        while True:
            request_id = take_restart_request()
            last_good = LAST_GOOD_PATH.read_bytes()
            staged = NEXT_PATH.is_file()
            candidate = NEXT_PATH.read_bytes() if staged else read_stable_candidate()
            if not apply_candidate(candidate, last_good, request_id, staged=staged):
                return 1
            if NEXT_PATH.is_file() or CONFIG_PATH.read_bytes() != LAST_GOOD_PATH.read_bytes():
                continue
            # A request written during restart remains for the next path trigger.
            return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="[%(name)s] %(message)s")
    try:
        raise SystemExit(main())
    except Exception as exc:
        LOGGER.error("Configuration application failed (%s)", type(exc).__name__)
        try:
            record_status("rollback_failed", CONFIG_PATH.read_bytes(), None, reason="Configuration controller failed")
        except OSError:
            LOGGER.error("Configuration status could not be written")
        raise SystemExit(1) from None
