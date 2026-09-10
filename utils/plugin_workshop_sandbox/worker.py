"""Container-only CLI: bounded UTF-8 JSON on stdin, one JSON report on stdout.

Run via DockerSandbox; this program deliberately refuses ordinary host execution.
The worker's assertions are untrusted functional evidence, not a security boundary.
"""

from __future__ import annotations

import os
import re
import sys
import json
import asyncio
from pathlib import Path, PurePosixPath
import platform
import traceback
import importlib.metadata

PROTOCOL = "1"
MAX_INPUT = 2 * 1024 * 1024


def read_request() -> dict:
    # Detached interactive containers may keep stdin open after the CLI writer closes.
    raw = sys.stdin.buffer.readline(MAX_INPUT + 1)
    if len(raw) > MAX_INPUT:
        raise ValueError("Worker input exceeds its byte limit")
    payload = json.loads(raw)
    if not isinstance(payload, dict) or payload.get("protocol") != PROTOCOL:
        raise ValueError("Unsupported workshop protocol")
    if not re.fullmatch(r"[a-z][a-z0-9_]{2,39}", payload.get("plugin_name", "")):
        raise ValueError("Invalid candidate module name")
    if not re.fullmatch(r"[a-f0-9]{64}", payload.get("source_hash", "")):
        raise ValueError("Invalid candidate hash")
    files = payload.get("files")
    if not isinstance(files, dict) or not 1 <= len(files) <= 32 or "__init__.py" not in files:
        raise ValueError("Invalid candidate files")
    total = 0
    seen = set()
    for name, source in files.items():
        if not isinstance(name, str) or not isinstance(source, str):
            raise ValueError("Candidate files must contain UTF-8 text")
        path = PurePosixPath(name)
        if (
            path.is_absolute()
            or path.as_posix() != name
            or any(part in {".", ".."} for part in path.parts)
            or "\\" in name
            or ":" in name
            or any(ord(character) < 32 for character in name)
            or name.casefold() in seen
        ):
            raise ValueError("Candidate file path is unsafe")
        seen.add(name.casefold())
        size = len(source.encode("utf-8"))
        if size > 65536:
            raise ValueError("Candidate file exceeds its byte limit")
        total += size
    if total > 256 * 1024:
        raise ValueError("Candidate package exceeds its byte limit")
    manifest = payload.get("manifest")
    if not isinstance(manifest, dict) or not isinstance(manifest.get("checks"), list):
        raise ValueError("Invalid candidate manifest")
    if not 1 <= len(manifest["checks"]) <= 24 or not any(check.get("repeatable", True) for check in manifest["checks"]):
        raise ValueError("Candidate needs at least one repeatable acceptance check")
    return payload


def main() -> int:
    if (
        sys.platform != "linux"
        or not hasattr(os, "getuid")
        or os.getuid() != 65532
        or Path.cwd() != Path("/workspace")
        or not Path("/.dockerenv").is_file()
    ):
        sys.stderr.write("The acceptance worker may only run in its explicit nonroot Docker container.\n")
        return 2
    python_version = platform.python_version()
    framework_version = importlib.metadata.version("arclet-entari")
    # Remove even image-provided environment configuration before framework imports.
    os.environ.clear()
    os.environ.update(
        {
            "HOME": "/workspace",
            "TMPDIR": "/tmp",
            "PATH": "/opt/workshop/.venv/bin:/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
        }
    )
    sys.dont_write_bytecode = True
    payload = read_request()
    if not python_version.startswith("3.10.") or payload["framework_version"] != framework_version:
        raise RuntimeError("Worker Python/Entari provenance does not match the host contract")
    # Reserve report output before redirecting all framework/candidate stdout to the
    # bounded log pipe. Malicious native code can still forge this in-process report.
    report_fd = os.dup(sys.stdout.fileno())
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from worker_harness import Acceptance

    acceptance = Acceptance(payload, Path("/workspace"))
    try:
        asyncio.run(acceptance.run())
    except BaseException as exc:
        traceback.print_exc(limit=12)
        acceptance.fail(exc)
    report = {
        "protocol": PROTOCOL,
        "source_hash": payload["source_hash"],
        "python_version": python_version,
        "framework_version": framework_version,
        "checks": acceptance.results,
        "status": "passed" if acceptance.results and all(item["passed"] for item in acceptance.results) else "failed",
    }
    with os.fdopen(report_fd, "w", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, allow_nan=False)
        output.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
