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
import tarfile
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
    source_archive: bool = False

    @property
    def output(self) -> Path:
        return ROOT / "vendor" / self.directory / f"{self.package}-{self.patched}-py3-none-any.whl"


PACKAGES = {
    "entari": WheelSpec(
        "arclet_entari",
        "0.19.0rc3",
        "0.19.0rc3+chtholly.1",
        "https://codeload.github.com/ArcletProject/Entari/tar.gz/c6b90174e694ecf6ba0629c1094ecc34fb32678f",
        "e0e9b3c245cf98175331cd4e1ffb1039802f99e1c0597730035086af5d3844a5",
        "entari-0.19.0rc3-native-reload.patch",
        "entari",
        source_archive=True,
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
    "llm": WheelSpec(
        "entari_plugin_llm",
        "0.2.2",
        "0.2.2+chtholly.1",
        "https://codeload.github.com/ArcletProject/entari-plugin-llm/tar.gz/e5675994de08dc24ad474812bd8c38b916b19d61",
        "aca5f5d70015709d7daa7ac04316145ec41c1b7de31228c69a4938b616d4831d",
        "llm-0.2.2-native-lifecycle.patch",
        "llm",
        source_archive=True,
    ),
    "webui": WheelSpec(
        "entari_plugin_webui",
        "1.0.3",
        "1.0.3+chtholly.4",
        "https://files.pythonhosted.org/packages/17/0c/"
        "c02c08d5f23ee7577f0ce6613aedb78c4e01740cbbad34299b2129c32d1e/"
        "entari_plugin_webui-1.0.3-py3-none-any.whl",
        "43f7a67034c23490c18547ba840bb9f97a39f49f58bb10c5835456fc377c6341",
        "webui-1.0.3-native-lifecycle.patch",
        "webui",
    ),
}

MAX_WHEEL_BYTES = 16 * 1024 * 1024
DOWNLOAD_TIMEOUT = 30
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
BUILD_BACKEND = "pdm-backend==2.5.0"


class BuildError(Exception):
    """An input or external tool prevented the pinned wheel rebuild."""


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise BuildError("upstream download redirected; only the pinned URL is allowed")


def read_upstream(spec: WheelSpec, upstream: Path | None) -> bytes:
    if upstream is not None:
        with upstream.open("rb") as stream:
            data = stream.read(MAX_WHEEL_BYTES + 1)
    else:
        opener = urllib.request.build_opener(NoRedirects())
        with opener.open(spec.url, timeout=DOWNLOAD_TIMEOUT) as response:
            data = response.read(MAX_WHEEL_BYTES + 1)
    if len(data) > MAX_WHEEL_BYTES:
        raise BuildError(f"upstream distribution exceeds the {MAX_WHEEL_BYTES}-byte limit")
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


def extract_source(data: bytes, destination: Path) -> Path:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        roots: set[str] = set()
        seen: set[str] = set()
        expanded_bytes = 0
        members = archive.getmembers()
        links: list[tuple[tarfile.TarInfo, tarfile.TarInfo]] = []
        for member in members:
            parts = member.name.rstrip("/").split("/")
            if (
                "\\" in member.name
                or ":" in member.name
                or PurePosixPath(member.name).is_absolute()
                or any(part in ("", ".", "..") or part.endswith((".", " ")) for part in parts)
                or not (member.isdir() or member.isfile() or member.issym())
            ):
                raise BuildError(f"unsafe source archive member: {member.name!r}")
            key = "/".join(parts).casefold()
            if key in seen:
                raise BuildError(f"duplicate source archive member: {member.name!r}")
            seen.add(key)
            roots.add(parts[0])
            expanded_bytes += member.size
            if member.issym():
                link = PurePosixPath(member.linkname)
                if (
                    link.is_absolute()
                    or any(part == ".." for part in link.parts)
                    or any(marker in member.linkname for marker in ("\\", ":"))
                ):
                    raise BuildError(f"unsafe source link: {member.name!r}")
                target = archive.getmember(str(PurePosixPath(member.name).parent / link))
                if not target.isfile():
                    raise BuildError(f"source link must target a regular archive file: {member.name!r}")
                expanded_bytes += target.size
                links.append((member, target))
            if expanded_bytes > MAX_WHEEL_BYTES:
                raise BuildError(f"expanded source exceeds the {MAX_WHEEL_BYTES}-byte limit")
        if len(roots) != 1:
            raise BuildError("source archive must contain exactly one project root")
        archive.extractall(destination, members=[member for member in members if not member.issym()])
        for member, target in links:
            stream = archive.extractfile(target)
            if stream is None:
                raise BuildError(f"source link target is unreadable: {member.name!r}")
            with stream:
                (destination / member.name).write_bytes(stream.read())
    source = destination / roots.pop()
    if not (source / "pyproject.toml").is_file():
        raise BuildError("source archive does not contain a Python project")
    return source


def build_source_wheel(spec: WheelSpec, data: bytes, workspace: Path) -> bytes:
    """Build only hash-verified, commit-pinned sources through a fixed backend."""
    source = extract_source(data, workspace / "source")
    constraints = workspace / "build-constraints.txt"
    constraints.write_text(BUILD_BACKEND + "\n", encoding="utf-8")
    output = workspace / "dist"
    command = [
        "uv",
        "build",
        "--no-config",
        "--no-sources",
        "--python",
        "3.10",
        "--wheel",
        "--build-constraints",
        str(constraints),
        "--out-dir",
        str(output),
        str(source),
    ]
    result = subprocess.run(
        command,
        cwd=workspace,
        env={**os.environ, "SOURCE_DATE_EPOCH": "315532800"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )
    if result.returncode:
        raise BuildError(f"upstream source build failed: {(result.stderr or result.stdout).strip()}")
    expected = output / f"{spec.package}-{spec.upstream}-py3-none-any.whl"
    if list(output.glob("*.whl")) != [expected]:
        raise BuildError("upstream source build did not produce exactly the pinned wheel")
    with expected.open("rb") as stream:
        wheel = stream.read(MAX_WHEEL_BYTES + 1)
    if len(wheel) > MAX_WHEEL_BYTES:
        raise BuildError(f"built wheel exceeds the {MAX_WHEEL_BYTES}-byte limit")
    return wheel


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


def install_webui_theme(source: Path) -> None:
    """Add content-addressed appearance assets without rewriting upstream application logic."""
    frontend = source / "entari_plugin_webui" / "static" / "frontend"
    entry = frontend / "index.html"
    document = entry.read_text(encoding="utf-8")
    if document.count('<html lang="zh-CN">') != 1 or document.count("</head>") != 1:
        raise BuildError("upstream WebUI entry changed; review theme integration before rebuilding")
    theme = ROOT / "patches" / "webui-theme"
    stylesheet = (ROOT / "utils" / "webui_theme" / "tokens.css").read_text(encoding="utf-8")
    stylesheet += "\n" + (theme / "native.css").read_text(encoding="utf-8")
    assets = {"css": stylesheet, "js": (theme / "native.js").read_text(encoding="utf-8")}
    names = {}
    for extension, content in assets.items():
        data = content.encode("utf-8")
        name = f"chtholly-theme-{hashlib.sha256(data).hexdigest()[:16]}.{extension}"
        (frontend / "assets" / name).write_bytes(data)
        names[extension] = name
    document = document.replace('<html lang="zh-CN">', '<html lang="zh-CN" data-ui-native>')
    document = document.replace(
        "</head>",
        f'<link rel="stylesheet" href="/assets/{names["css"]}">\n'
        f'<script defer src="/assets/{names["js"]}"></script>\n</head>',
    )
    entry.write_bytes(document.encode("utf-8"))


def build(spec: WheelSpec, upstream: Path | None = None, output: Path | None = None) -> Path:
    """Verify, build, patch and atomically publish a pinned distribution."""
    output = output or spec.output
    data = read_upstream(spec, upstream)
    with tempfile.TemporaryDirectory(prefix="chtholly-wheel-") as directory:
        workspace = Path(directory).resolve()
        if spec.source_archive:
            data = build_source_wheel(spec, data, workspace)
        source = workspace / "wheel"
        extract_upstream(data, source)
        apply_patch(spec, source)
        if spec.directory == "webui":
            install_webui_theme(source)
        update_metadata(spec, source)
        publish_wheel(spec, source, output)
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", choices=PACKAGES, required=True)
    parser.add_argument("--upstream", type=Path, help="local pinned wheel or source archive (verified by SHA256)")
    parser.add_argument("--output", type=Path, help="override the vendored output wheel")
    args = parser.parse_args(argv)
    try:
        output = build(PACKAGES[args.package], args.upstream, args.output)
    except (
        BuildError,
        OSError,
        ValueError,
        SyntaxError,
        zipfile.BadZipFile,
        tarfile.TarError,
        subprocess.SubprocessError,
    ) as exc:
        sys.stderr.write(f"build_patched_wheel: {exc}\n")
        return 1
    sys.stdout.write(f"{output}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
