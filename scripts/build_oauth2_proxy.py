#!/usr/bin/env python3
"""Build the reviewed OAuth2 Proxy release in a disposable, isolated workspace."""

from __future__ import annotations

import io
import os
import re
import sys
import json
import shutil
import hashlib
from pathlib import Path, PurePosixPath
import tarfile
import zipfile
import argparse
import platform
import tempfile
import subprocess
import urllib.request

ASSETS = Path(__file__).resolve().parent / "oauth2_proxy"
ROOT = ASSETS.parents[1]
MANIFEST = ASSETS / "manifest.json"
MAX_DOWNLOAD_BYTES = 160 * 1024 * 1024
MAX_EXTRACT_BYTES = 800 * 1024 * 1024
TEST_PACKAGES = (
    "./pkg/middleware",
    "./providers",
    "./pkg/sessions/...",
    "./pkg/requests",
    "./pkg/apis/sessions",
    "./pkg/providers/oidc",
    ".",
)
HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:.*)\n")


class BuildError(Exception):
    """Pinned inputs or an external command could not be verified."""


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BuildError("download redirected away from the pinned official URL")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def download(url: str, expected: str) -> bytes:
    if not url.startswith(("https://codeload.github.com/oauth2-proxy/oauth2-proxy/", "https://dl.google.com/go/")):
        raise BuildError("download URL is not an approved official source")
    opener = urllib.request.build_opener(NoRedirects())
    with opener.open(url, timeout=90) as response:
        data = response.read(MAX_DOWNLOAD_BYTES + 1)
    if len(data) > MAX_DOWNLOAD_BYTES or sha256(data) != expected:
        raise BuildError("download size/checksum mismatch")
    return data


def safe_name(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
        raise BuildError(f"unsafe archive/patch path: {name!r}")
    return path


def extract(data: bytes, destination: Path, *, zipped: bool = False) -> None:
    total = 0
    names: set[str] = set()
    if zipped:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for member in archive.infolist():
                name = safe_name(member.filename)
                if member.filename in names:
                    raise BuildError("duplicate archive entry")
                names.add(member.filename)
                total += member.file_size
                if total > MAX_EXTRACT_BYTES or (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise BuildError("oversized archive or symbolic link")
                target = destination.joinpath(*name.parts)
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.read(member))
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            for member in archive:
                name = safe_name(member.name)
                if member.name in names:
                    raise BuildError("duplicate archive entry")
                names.add(member.name)
                total += member.size
                if total > MAX_EXTRACT_BYTES or not (member.isfile() or member.isdir()):
                    raise BuildError("oversized archive or non-regular entry")
                target = destination.joinpath(*name.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise BuildError("unreadable archive member")
                    with stream:
                        target.write_bytes(stream.read())
                    target.chmod(member.mode & 0o755)


def apply_patch(source: Path, manifest: dict) -> None:
    patch = (ASSETS / manifest["patch"]).read_bytes()
    if sha256(patch) != manifest["patch_sha256"]:
        raise BuildError("reviewed patch checksum mismatch")
    lines = patch.decode("utf-8").splitlines(keepends=True)
    index = 0
    applied: set[str] = set()
    while index < len(lines):
        if not lines[index].startswith("--- ") or index + 1 >= len(lines) or not lines[index + 1].startswith("+++ b/"):
            raise BuildError("invalid unified patch header")
        old_name = lines[index][4:].rstrip("\n")
        name = lines[index + 1][6:].rstrip("\n")
        safe_name(name)
        if name not in manifest["files"] or name in applied:
            raise BuildError("patch touches an unlisted or repeated file")
        expected = manifest["files"][name]
        path = source.joinpath(*PurePosixPath(name).parts)
        if expected["before"] is None:
            if old_name != "/dev/null" or path.exists():
                raise BuildError(f"new patch file already exists: {name}")
            before = b""
        else:
            if old_name != "a/" + name:
                raise BuildError("patch source path mismatch")
            before = path.read_bytes()
            if sha256(before) != expected["before"]:
                raise BuildError(f"upstream patch precondition failed: {name}")
        original = before.decode("utf-8").splitlines(keepends=True)
        result: list[str] = []
        cursor = 0
        index += 2
        while index < len(lines) and lines[index].startswith("@@ "):
            match = HUNK.fullmatch(lines[index])
            if not match:
                raise BuildError("invalid patch hunk header")
            old_line, old_count, new_line, new_count = match.groups()
            old_count = int(old_count) if old_count is not None else 1
            new_count = int(new_count) if new_count is not None else 1
            offset = int(old_line) - (1 if old_count else 0)
            if offset < cursor or offset > len(original):
                raise BuildError("overlapping or out-of-range patch hunk")
            result.extend(original[cursor:offset])
            cursor = offset
            if len(result) != int(new_line) - (1 if new_count else 0):
                raise BuildError("patch output position mismatch")
            consumed = produced = 0
            index += 1
            while consumed < old_count or produced < new_count:
                if index >= len(lines):
                    raise BuildError("truncated patch")
                line = lines[index]
                index += 1
                if line[:1] not in (" ", "+", "-"):
                    raise BuildError("invalid patch row")
                if line[0] in " -":
                    if cursor >= len(original) or original[cursor] != line[1:]:
                        raise BuildError(f"exact patch context mismatch: {name}")
                    cursor += 1
                    consumed += 1
                if line[0] in " +":
                    result.append(line[1:])
                    produced += 1
                if consumed > old_count or produced > new_count:
                    raise BuildError("patch row counts exceed hunk header")
        result.extend(original[cursor:])
        after = "".join(result).encode("utf-8")
        if sha256(after) != expected["after"]:
            raise BuildError(f"patched source checksum mismatch: {name}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(after)
        applied.add(name)
    if applied != set(manifest["files"]):
        raise BuildError("patch omitted a reviewed file")


def external_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved == ROOT or ROOT in resolved.parents:
        raise BuildError("build workspace and output must be outside the project")
    return resolved


def command(argv: list[str], source: Path, environment: dict[str, str]) -> str:
    result = subprocess.run(
        argv,
        cwd=source,
        env=environment,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=1800,
    )
    if result.stdout:
        sys.stdout.write(result.stdout)
        sys.stdout.flush()
    return result.stdout


def build(output_dir: Path, work_root: Path | None, target_os: str, target_arch: str, test: bool) -> Path:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    output_dir = external_path(output_dir)
    parent = external_path(work_root) if work_root else external_path(Path(tempfile.gettempdir()))
    parent.mkdir(parents=True, exist_ok=True)
    host_os = {"Windows": "windows", "Linux": "linux"}.get(platform.system())
    host_arch = {"AMD64": "amd64", "x86_64": "amd64", "aarch64": "arm64", "ARM64": "arm64"}.get(platform.machine())
    host = f"{host_os}-{host_arch}"
    if host not in manifest["toolchains"]:
        raise BuildError(f"no reviewed Go toolchain for host {host}")
    toolchain = manifest["toolchains"][host]
    with tempfile.TemporaryDirectory(prefix="chtholly-oauth2-build-", dir=parent) as directory:
        workspace = Path(directory)
        source_data = download(manifest["source_url"], manifest["source_sha256"])
        extract(source_data, workspace)
        source = workspace / ("oauth2-proxy-" + manifest["source_commit"])
        if (
            sha256((source / "go.mod").read_bytes()) != manifest["go_mod_sha256"]
            or sha256((source / "go.sum").read_bytes()) != manifest["go_sum_sha256"]
        ):
            raise BuildError("upstream dependency lock changed")
        apply_patch(source, manifest)
        toolchain_url = "https://dl.google.com/go/" + toolchain["filename"]
        toolchain_data = download(toolchain_url, toolchain["sha256"])
        extract(toolchain_data, workspace, zipped=toolchain["filename"].endswith(".zip"))
        go = workspace / "go" / "bin" / ("go.exe" if host_os == "windows" else "go")
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GO") and key not in ("CGO_ENABLED", "CC", "CXX", "PKG_CONFIG")
        }
        environment.update(
            {
                "GOROOT": str(workspace / "go"),
                "GOPATH": str(workspace / "gopath"),
                "GOMODCACHE": str(workspace / "modules"),
                "GOCACHE": str(workspace / "cache"),
                "GOTMPDIR": str(workspace / "tmp"),
                "GOENV": "off",
                "GOWORK": "off",
                "GOTOOLCHAIN": "local",
                "GOPROXY": "https://proxy.golang.org",
                "GOSUMDB": "sum.golang.org",
                "GONOSUMDB": "",
                "GONOPROXY": "",
                "GOPRIVATE": "",
                "GOVCS": "*:off",
                "CGO_ENABLED": "0",
                "GOFLAGS": "-mod=readonly",
                "GOAMD64": "v1",
                "GOARM64": "v8.0",
                "GOTELEMETRY": "off",
            }
        )
        (workspace / "tmp").mkdir()
        version = command([str(go), "version"], source, environment).strip()
        if version != f"go version {manifest['go_version']} {host_os}/{host_arch}":
            raise BuildError("extracted compiler does not match reviewed toolchain")
        command([str(go), "mod", "download"], source, environment)
        command([str(go), "mod", "verify"], source, environment)
        if test:
            command([str(go), "test", "-count=1", "-timeout=15m", *TEST_PACKAGES], source, environment)
        environment.update({"GOOS": target_os, "GOARCH": target_arch})
        binary_name = "oauth2-proxy.exe" if target_os == "windows" else "oauth2-proxy"
        binary = workspace / binary_name
        args = [
            str(go),
            "build",
            "-trimpath",
            "-buildvcs=false",
            "-ldflags",
            "-s -w -buildid= -X github.com/oauth2-proxy/oauth2-proxy/v7/pkg/version.VERSION="
            + manifest["patched_version"],
            "-o",
            str(binary),
            ".",
        ]
        command(args, source, environment)
        if (
            sha256((source / "go.mod").read_bytes()) != manifest["go_mod_sha256"]
            or sha256((source / "go.sum").read_bytes()) != manifest["go_sum_sha256"]
        ):
            raise BuildError("build changed pinned dependencies")
        provenance = {
            "version": manifest["patched_version"],
            "source_url": manifest["source_url"],
            "source_commit": manifest["source_commit"],
            "source_sha256": manifest["source_sha256"],
            "patch_sha256": manifest["patch_sha256"],
            "manifest_sha256": sha256(MANIFEST.read_bytes()),
            "go_version": version,
            "toolchain_url": toolchain_url,
            "toolchain_sha256": toolchain["sha256"],
            "go_mod_sha256": manifest["go_mod_sha256"],
            "go_sum_sha256": manifest["go_sum_sha256"],
            "target": f"{target_os}/{target_arch}",
            "binary_sha256": sha256(binary.read_bytes()),
            "tests": list(TEST_PACKAGES) if test else [],
            "cgo": False,
            "build_flags": args[2:-3],
            "patched_files": manifest["files"],
        }
        output_dir.mkdir(parents=True, exist_ok=True)
        target = output_dir / binary_name
        provenance_path = output_dir / (binary_name + ".provenance.json")
        if target.exists() or provenance_path.exists():
            raise BuildError("output exists; use a fresh output directory")
        temporary = output_dir / (binary_name + ".new")
        try:
            shutil.copyfile(binary, temporary)
            temporary.chmod(0o755)
            temporary.replace(target)
            provenance_path.write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        finally:
            temporary.unlink(missing_ok=True)
        sys.stdout.write(f"Built {target}\nProvenance {provenance_path}\n")
        return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path, help="fresh directory outside the project/runtime")
    parser.add_argument("--work-root", type=Path, help="external parent for disposable source/toolchain/caches")
    parser.add_argument("--target-os", choices=("linux", "windows"), default="linux")
    parser.add_argument("--target-arch", choices=("amd64", "arm64"), default="amd64")
    parser.add_argument("--skip-tests", action="store_true", help="build without regressions; recorded in provenance")
    args = parser.parse_args(argv)
    try:
        build(args.output_dir, args.work_root, args.target_os, args.target_arch, not args.skip_tests)
    except subprocess.CalledProcessError as exc:
        sys.stderr.write(exc.stdout or "")
        parser.exit(1, f"build command failed with status {exc.returncode}\n")
    except (BuildError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        parser.exit(1, f"build failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
