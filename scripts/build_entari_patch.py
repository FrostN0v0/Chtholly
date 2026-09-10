#!/usr/bin/env python3
"""Rebuild Chtholly's Entari wheel from a pinned upstream wheel and local patch."""

from __future__ import annotations

import io
import os
import csv
import sys
import stat
import base64
import hashlib
from pathlib import Path, PurePosixPath
import zipfile
import argparse
import tempfile
import subprocess
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_VERSION = "0.19.0rc2"
PATCHED_VERSION = "0.19.0rc2+chtholly.2"
UPSTREAM_URL = (
    "https://files.pythonhosted.org/packages/60/9d/"
    "77b1d45b57f02dc8ac88d025c92c52060e5ad0b7ce8e0db5102cd8455fd0/"
    "arclet_entari-0.19.0rc2-py3-none-any.whl"
)
UPSTREAM_SHA256 = "f6c1a568d63034ab32ddf83d8500cb3e8fdadff3f629f15a04aa74f63f7aa7b5"
PATCH = ROOT / "patches" / "entari-0.19.0rc2-staged-rollback.patch"
WHEEL_NAME = f"arclet_entari-{PATCHED_VERSION}-py3-none-any.whl"
DEFAULT_OUTPUT = ROOT / "vendor" / "entari" / WHEEL_NAME
MAX_WHEEL_BYTES = 16 * 1024 * 1024
DOWNLOAD_TIMEOUT = 30
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


class BuildError(Exception):
    """An input or external tool prevented the pinned wheel rebuild."""


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BuildError("upstream download redirected; only the pinned URL is allowed")


def read_upstream(wheel: Path | None) -> bytes:
    if wheel is not None:
        with wheel.open("rb") as stream:
            data = stream.read(MAX_WHEEL_BYTES + 1)
    else:
        opener = urllib.request.build_opener(NoRedirects())
        with opener.open(UPSTREAM_URL, timeout=DOWNLOAD_TIMEOUT) as response:
            data = response.read(MAX_WHEEL_BYTES + 1)
    if len(data) > MAX_WHEEL_BYTES:
        raise BuildError(f"upstream wheel exceeds the {MAX_WHEEL_BYTES}-byte limit")
    digest = hashlib.sha256(data).hexdigest()
    if digest != UPSTREAM_SHA256:
        raise BuildError(f"upstream SHA256 mismatch: expected {UPSTREAM_SHA256}, got {digest}")
    return data


def extract_upstream(data: bytes, destination: Path) -> None:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        seen: set[str] = set()
        for member in archive.infolist():
            name = member.filename
            parts = name.rstrip("/").split("/")
            mode = stat.S_IFMT(member.external_attr >> 16)
            if (
                name != member.orig_filename
                or "\\" in name
                or ":" in name
                or PurePosixPath(name).is_absolute()
                or any(part in ("", ".", "..") for part in parts)
                or any(part.endswith((".", " ")) for part in parts)
                or mode not in (0, stat.S_IFREG, stat.S_IFDIR)
            ):
                raise BuildError(f"unsafe wheel member: {name!r}")
            key = "/".join(parts).casefold()
            if key in seen:
                raise BuildError(f"duplicate wheel member: {name!r}")
            seen.add(key)
            if not (destination / name).resolve().is_relative_to(destination):
                raise BuildError(f"wheel member escapes extraction directory: {name!r}")
        archive.extractall(destination)


def apply_patch(source: Path) -> None:
    if not PATCH.is_file():
        raise BuildError(f"patch not found: {PATCH}")
    for check in (True, False):
        command = ["git", "-c", "core.autocrlf=false", "apply"]
        if check:
            command.append("--check")
        command.extend(["--", str(PATCH)])
        result = subprocess.run(
            command,
            cwd=source,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        if result.returncode:
            stage = "check" if check else "application"
            detail = (result.stderr or result.stdout).strip()
            raise BuildError(f"patch {stage} failed: {detail}")
    for path in sorted(source.rglob("*.py")):
        compile(path.read_bytes(), path.relative_to(source).as_posix(), "exec", dont_inherit=True)


def update_metadata(source: Path) -> None:
    old_info = source / f"arclet_entari-{UPSTREAM_VERSION}.dist-info"
    metadata = old_info / "METADATA"
    lines = metadata.read_bytes().splitlines(keepends=True)
    old_version = f"Version: {UPSTREAM_VERSION}".encode("ascii")
    new_version = f"Version: {PATCHED_VERSION}".encode("ascii")
    changed = 0
    for index, line in enumerate(lines):
        if not line.strip():
            break
        if line.rstrip(b"\r\n") == old_version:
            lines[index] = line.replace(old_version, new_version, 1)
            changed += 1
    if changed != 1:
        raise BuildError("upstream METADATA must contain exactly one expected Version header")
    metadata.write_bytes(b"".join(lines))
    old_info.rename(source / f"arclet_entari-{PATCHED_VERSION}.dist-info")


def publish_wheel(source: Path, output: Path) -> None:
    record_name = f"arclet_entari-{PATCHED_VERSION}.dist-info/RECORD"
    files = sorted(
        (path.relative_to(source).as_posix(), path)
        for path in source.rglob("*")
        if path.is_file() and path.relative_to(source).as_posix() != record_name
    )
    record = io.StringIO(newline="")
    writer = csv.writer(record, lineterminator="\n")
    for name, path in files:
        data = path.read_bytes()
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
        writer.writerow((name, f"sha256={digest}", len(data)))
    writer.writerow((record_name, "", ""))
    record_path = source / record_name
    record_path.write_bytes(record.getvalue().encode("utf-8"))
    files.append((record_name, record_path))
    files.sort()

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{output.name}.", suffix=".tmp", dir=output.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            # Stored entries avoid compressor-version differences across rebuild hosts.
            with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
                for name, path in files:
                    member = zipfile.ZipInfo(name, date_time=ZIP_TIMESTAMP)
                    member.create_system = 3
                    member.external_attr = (stat.S_IFREG | 0o644) << 16
                    member.compress_type = zipfile.ZIP_STORED
                    archive.writestr(member, path.read_bytes())
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def build(wheel: Path | None = None, output: Path = DEFAULT_OUTPUT) -> Path:
    """Verify, patch and atomically publish the fixed Entari wheel without importing it."""
    data = read_upstream(wheel)
    with tempfile.TemporaryDirectory(prefix="chtholly-entari-") as directory:
        source = Path(directory).resolve()
        extract_upstream(data, source)
        apply_patch(source)
        update_metadata(source)
        publish_wheel(source, output)
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, help="local official wheel (must match the pinned SHA256)")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help=f"output wheel (default: {DEFAULT_OUTPUT})")
    args = parser.parse_args(argv)
    try:
        output = build(args.wheel, args.output)
    except (BuildError, OSError, ValueError, SyntaxError, zipfile.BadZipFile, subprocess.SubprocessError) as exc:
        sys.stderr.write(f"build_entari_patch: {exc}\n")
        return 1
    sys.stdout.write(f"{output}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
