#!/usr/bin/env python3
"""Rebuild pinned Entari ecosystem wheels with audited repository patches."""

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
from dataclasses import dataclass
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class WheelSpec:
    package: str
    upstream: str
    patched: str
    url: str
    sha256: str
    patch: str
    directory: str

    @property
    def output(self) -> Path:
        return ROOT / "vendor" / self.directory / f"{self.package}-{self.patched}-py3-none-any.whl"


PACKAGES = {
    "entari": WheelSpec(
        "arclet_entari",
        "0.19.0rc2",
        "0.19.0rc2+chtholly.2",
        "https://files.pythonhosted.org/packages/60/9d/"
        "77b1d45b57f02dc8ac88d025c92c52060e5ad0b7ce8e0db5102cd8455fd0/"
        "arclet_entari-0.19.0rc2-py3-none-any.whl",
        "f6c1a568d63034ab32ddf83d8500cb3e8fdadff3f629f15a04aa74f63f7aa7b5",
        "entari-0.19.0rc2-staged-rollback.patch",
        "entari",
    ),
    "htmlrender": WheelSpec(
        "entari_plugin_htmlrender",
        "0.1.0",
        "0.1.0+chtholly.1",
        "https://files.pythonhosted.org/packages/0d/d1/"
        "229c5569dc41eb5ab82ae94b2ca98803f266c6f2390712dd094173024694/"
        "entari_plugin_htmlrender-0.1.0-py3-none-any.whl",
        "c982d3e8846f80598d66bd5039810d0d21303070b8a153014457ec4a6782c9fb",
        "htmlrender-0.1.0-browser-cache.patch",
        "htmlrender",
    ),
    "webui": WheelSpec(
        "entari_plugin_webui",
        "1.0.3",
        "1.0.3+chtholly.1",
        "https://files.pythonhosted.org/packages/17/0c/"
        "c02c08d5f23ee7577f0ce6613aedb78c4e01740cbbad34299b2129c32d1e/"
        "entari_plugin_webui-1.0.3-py3-none-any.whl",
        "43f7a67034c23490c18547ba840bb9f97a39f49f58bb10c5835456fc377c6341",
        "webui-1.0.3-connection-lifecycle.patch",
        "webui",
    ),
}

MAX_WHEEL_BYTES = 16 * 1024 * 1024
DOWNLOAD_TIMEOUT = 30
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


class BuildError(Exception):
    """An input or external tool prevented the pinned wheel rebuild."""


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BuildError("upstream download redirected; only the pinned URL is allowed")


def read_upstream(spec: WheelSpec, wheel: Path | None) -> bytes:
    if wheel is not None:
        with wheel.open("rb") as stream:
            data = stream.read(MAX_WHEEL_BYTES + 1)
    else:
        opener = urllib.request.build_opener(NoRedirects())
        with opener.open(spec.url, timeout=DOWNLOAD_TIMEOUT) as response:
            data = response.read(MAX_WHEEL_BYTES + 1)
    if len(data) > MAX_WHEEL_BYTES:
        raise BuildError(f"upstream wheel exceeds the {MAX_WHEEL_BYTES}-byte limit")
    digest = hashlib.sha256(data).hexdigest()
    if digest != spec.sha256:
        raise BuildError(f"upstream SHA256 mismatch: expected {spec.sha256}, got {digest}")
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


def apply_patch(spec: WheelSpec, source: Path) -> None:
    patch = ROOT / "patches" / spec.patch
    if not patch.is_file():
        raise BuildError(f"patch not found: {patch}")
    for check in (True, False):
        command = ["git", "-c", "core.autocrlf=false", "apply"]
        if check:
            command.append("--check")
        command.extend(["--", str(patch)])
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


def update_metadata(spec: WheelSpec, source: Path) -> None:
    old_info = source / f"{spec.package}-{spec.upstream}.dist-info"
    metadata = old_info / "METADATA"
    lines = metadata.read_bytes().splitlines(keepends=True)
    old_version = f"Version: {spec.upstream}".encode("ascii")
    new_version = f"Version: {spec.patched}".encode("ascii")
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
    old_info.rename(source / f"{spec.package}-{spec.patched}.dist-info")


def publish_wheel(spec: WheelSpec, source: Path, output: Path) -> None:
    record_name = f"{spec.package}-{spec.patched}.dist-info/RECORD"
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


def build(spec: WheelSpec, wheel: Path | None = None, output: Path | None = None) -> Path:
    """Verify, patch and atomically publish a pinned wheel without importing it."""
    output = output or spec.output
    data = read_upstream(spec, wheel)
    with tempfile.TemporaryDirectory(prefix="chtholly-wheel-") as directory:
        source = Path(directory).resolve()
        extract_upstream(data, source)
        apply_patch(spec, source)
        update_metadata(spec, source)
        publish_wheel(spec, source, output)
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", choices=PACKAGES, required=True)
    parser.add_argument("--wheel", type=Path, help="local official wheel (must match the pinned SHA256)")
    parser.add_argument("--output", type=Path, help="override the vendored output wheel")
    args = parser.parse_args(argv)
    try:
        output = build(PACKAGES[args.package], args.wheel, args.output)
    except (BuildError, OSError, ValueError, SyntaxError, zipfile.BadZipFile, subprocess.SubprocessError) as exc:
        sys.stderr.write(f"build_patched_wheel: {exc}\n")
        return 1
    sys.stdout.write(f"{output}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
