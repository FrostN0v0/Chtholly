"""Explicit operator CLI for the isolated native plugin acceptance image.

Usage: python scripts/build_workshop_sandbox.py [--tag chtholly-workshop:local]
Only allowlisted trusted bytes enter the temporary Docker build context. No project
code, credentials, .env, resources, data, or checkout root is sent to Docker.
"""

from __future__ import annotations

import re
import sys
import json
import hashlib
from pathlib import Path
import zipfile
import argparse
import tempfile
import subprocess
from email.parser import BytesParser

DEFAULT_BASE = "ghcr.io/astral-sh/uv@sha256:a041b350d5d9483b538d5af07e9553ee8cbc7fc7fa90c2f7d20d93f18ce9bbd1"
WORKER_FILES = (
    "worker.py",
    "worker_harness.py",
    "worker_transport.py",
    "worker_rendering.py",
    "worker_http.py",
    "probe_plugin.py",
)


def docker_json(executable: str, *args: str):
    result = subprocess.run([executable, *args], check=True, capture_output=True, timeout=60)
    return json.loads(result.stdout)


def trusted_bytes(root: Path, relative: str) -> bytes:
    target = root / relative
    current = target
    while current != root:
        if current.is_symlink():
            raise ValueError(f"Trusted build input is a symlink: {relative}")
        current = current.parent
    if not target.is_file():
        raise ValueError(f"Missing trusted build input: {relative}")
    return target.read_bytes()


def collect_inputs(root: Path) -> tuple[dict[str, bytes], str, str]:
    pyproject = trusted_bytes(root, "pyproject.toml")
    lock = trusted_bytes(root, "uv.lock")
    wheels = set(re.findall(r'vendor/entari/arclet_entari-[^"\s/]+\.whl', lock.decode("utf-8")))
    if len(wheels) != 1:
        raise ValueError("The current lock must identify exactly one vendored Entari wheel")
    wheel_path = wheels.pop()
    if wheel_path.encode() not in pyproject:
        raise ValueError("Project and lock do not reference the same pinned Entari wheel")
    wheel = trusted_bytes(root, wheel_path)
    with zipfile.ZipFile(root / wheel_path) as archive:
        metadata_names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            raise ValueError("Pinned Entari wheel has ambiguous package metadata")
        metadata = BytesParser().parsebytes(archive.read(metadata_names[0]))
        framework = metadata["Version"]
        if metadata["Name"].replace("_", "-").lower() != "arclet-entari" or not framework:
            raise ValueError("Pinned wheel is not an Entari distribution")
    inputs = {
        "Dockerfile": trusted_bytes(root, "scripts/workshop_sandbox/Dockerfile"),
        "pyproject.toml": pyproject,
        "uv.lock": lock,
        wheel_path: wheel,
    }
    webui_wheels = set(re.findall(r'vendor/webui/entari_plugin_webui-[^"\s/]+\.whl', lock.decode("utf-8")))
    if len(webui_wheels) != 1:
        raise ValueError("The current lock must identify exactly one vendored WebUI wheel")
    webui_path = webui_wheels.pop()
    if webui_path.encode() not in pyproject:
        raise ValueError("Project and lock do not reference the same pinned WebUI wheel")
    inputs[webui_path] = trusted_bytes(root, webui_path)
    for name in WORKER_FILES:
        inputs[f"worker/{name}"] = trusted_bytes(root, f"utils/plugin_workshop_sandbox/{name}")
    return inputs, framework, hashlib.sha256(wheel).hexdigest()


def resolve_base(executable: str, image: str) -> str:
    # Pulling/building is an explicit operator action here, never a model-call fallback.
    subprocess.run([executable, "pull", "--platform", "linux/amd64", image], check=True, timeout=600)
    inspected = docker_json(executable, "image", "inspect", image)[0]
    if inspected.get("Os") != "linux" or inspected.get("Architecture") != "amd64":
        raise ValueError("Builder requires a Linux amd64 Python 3.10/uv base")
    digests = inspected.get("RepoDigests") or []
    if "@sha256:" in image:
        if image not in digests:
            raise ValueError("Pulled base does not match the requested digest")
        return image
    repository = image.rsplit(":", 1)[0] if ":" in image.rsplit("/", 1)[-1] else image
    matches = [digest for digest in digests if digest.startswith(repository + "@sha256:")]
    if len(matches) != 1:
        raise ValueError("Cannot resolve the public base to one reproducible repository digest")
    return matches[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", default="chtholly-workshop:local")
    parser.add_argument("--docker", default="docker", help="Docker executable")
    parser.add_argument(
        "--base", default=DEFAULT_BASE, help="Public Python 3.10/uv image; use emitted digest to reproduce"
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    inputs, framework, wheel_hash = collect_inputs(root)
    base = resolve_base(args.docker, args.base)
    digest = hashlib.sha256()
    for name, content in sorted(inputs.items()):
        digest.update(name.encode("utf-8") + b"\0" + hashlib.sha256(content).digest())
    input_hash = digest.hexdigest()
    with tempfile.TemporaryDirectory(prefix="chtholly-workshop-build-") as temporary:
        context = Path(temporary)
        for name, content in inputs.items():
            destination = context / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        # Build context is the temporary allowlist, NEVER root or the current directory.
        subprocess.run(
            [
                args.docker,
                "build",
                "--platform",
                "linux/amd64",
                "--pull=false",
                "--tag",
                args.tag,
                "--build-arg",
                f"BASE_IMAGE={base}",
                "--build-arg",
                f"FRAMEWORK_VERSION={framework}",
                "--build-arg",
                f"LOCK_SHA256={hashlib.sha256(inputs['uv.lock']).hexdigest()}",
                "--build-arg",
                f"WHEEL_SHA256={wheel_hash}",
                "--build-arg",
                f"INPUT_SHA256={input_hash}",
                str(context),
            ],
            check=True,
            timeout=3600,
        )
    image = docker_json(args.docker, "image", "inspect", args.tag)[0]
    sys.stdout.write(
        json.dumps(
            {
                "image": args.tag,
                "image_id": image["Id"],
                "base": base,
                "framework_version": framework,
                "python_version": "3.10",
                "input_sha256": input_hash,
                "lock_sha256": hashlib.sha256(inputs["uv.lock"]).hexdigest(),
                "wheel_sha256": wheel_hash,
                "context_files": sorted(inputs),
            },
            indent=2,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
