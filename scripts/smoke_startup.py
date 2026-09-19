"""Exercise a clean source snapshot, real startup, and preserved upgrade state."""

from __future__ import annotations

import os
import sys
import json
import time
import shutil
import socket
from pathlib import Path
import sqlite3
import argparse
import tempfile
import subprocess
import urllib.error
import urllib.request

import psutil
from ruamel.yaml import YAML

ROOT = Path(__file__).resolve().parents[1]


class SmokeFailure(RuntimeError):
    """A clean-install contract was not satisfied."""


def environment(workspace: Path) -> dict[str, str]:
    allowed = {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "SYSTEMDRIVE",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "HOME",
        "LANG",
        "LC_ALL",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROGRAMDATA",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "UV_CACHE_DIR",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "PLAYWRIGHT_DOWNLOAD_HOST",
    }
    result = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    result.update(
        UV_PROJECT_ENVIRONMENT=str(workspace / ".venv"),
        PYTHONNOUSERSITE="1",
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONUTF8="1",
        PLAYWRIGHT_BROWSERS_PATH=str(workspace / "browser-cache"),
    )
    return result


def snapshot(workspace: Path) -> None:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    for name in set(result.stdout.decode("utf-8").split("\0")) - {""}:
        path = Path(name)
        if path.name in {".env", ".env.local", "entari.local.yml"} or (
            path.name.startswith(".env.") and path.name != ".env.example"
        ):
            continue
        source = ROOT / path
        if source.is_file():
            destination = workspace / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)


def run(workspace: Path, env: dict[str, str], *arguments: str, success: bool = True) -> str:
    result = subprocess.run(
        ["uv", *arguments],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=1800 if "--prepare" in arguments else 600,
    )
    if (result.returncode == 0) != success:
        raise SmokeFailure(f"Command {arguments[:3]!r} returned unexpected status {result.returncode}")
    return result.stdout + result.stderr


def terminate(process: subprocess.Popen) -> None:
    try:
        parent = psutil.Process(process.pid)
        children = parent.children(recursive=True)
    except psutil.NoSuchProcess:
        children = []
    for child in reversed(children):
        try:
            child.terminate()
        except psutil.NoSuchProcess:
            continue
    if process.poll() is None:
        process.terminate()
    _, alive = psutil.wait_procs(children, timeout=10)
    for child in alive:
        try:
            child.kill()
        except psutil.NoSuchProcess:
            continue
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def serve_once(workspace: Path, env: dict[str, str], port: int, *, resources: bool) -> None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with (workspace / "startup.log").open("wb") as log:
        process = subprocess.Popen(
            ["uv", "run", "--locked", "main.py"],
            cwd=workspace,
            env=env,
            stdout=log,
            stderr=log,
        )
        try:
            deadline = time.monotonic() + 90
            consecutive = 0
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise SmokeFailure("The real Bot exited before becoming ready")
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/satori/v1/meta",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                )
                try:
                    with opener.open(request, timeout=2) as response:
                        payload = json.load(response)
                        if response.status != 200 or payload.get("logins") != []:
                            raise SmokeFailure("Unexpected native Satori metadata response")
                        consecutive += 1
                except (urllib.error.URLError, TimeoutError):
                    consecutive = 0
                if consecutive >= 3:
                    if not resources:
                        return
                    try:
                        with opener.open(f"http://127.0.0.1:{port}/__startup_smoke__/render", timeout=20) as response:
                            rendered = json.load(response)
                            if rendered == {"browser": True, "htmlrender": True}:
                                return
                    except (urllib.error.URLError, TimeoutError):
                        pass
                time.sleep(0.5)
            raise SmokeFailure("Native Satori API did not become ready")
        finally:
            terminate(process)


def write_config(path: Path, config: dict) -> None:
    yaml = YAML()
    with path.open("w", encoding="utf-8") as stream:
        yaml.dump(config, stream)


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def exercise(workspace: Path, *, resources: bool) -> dict[str, object]:
    snapshot(workspace)
    env = environment(workspace)
    run(workspace, env, "sync", "--locked", "--all-extras")
    run(workspace, env, "run", "--locked", "main.py", "--check")
    if (workspace / "data").exists():
        raise SmokeFailure("Read-only check created the database directory")
    config = YAML(typ="safe").load((workspace / "entari.yml").read_text(encoding="utf-8"))
    port = free_port()
    config["plugins"]["server"]["port"] = port
    config["plugins"]["database"]["name"] = "persistent/chat.db"
    if resources:
        config["plugins"]["browser"] = {}
        config["plugins"]["htmlrender"] = {
            "provider": "playwright",
            "startup": "probe",
            "provider_config": {"engine": "chromium", "storage_path": str(workspace / "browser-cache")},
        }
        template_root = workspace / "smoke-templates"
        template_root.mkdir()
        (template_root / "sample.html").write_text(
            "<!doctype html><html><head><style>html,body{margin:0;width:32px;height:24px;"
            "background:#2468ac}</style></head><body></body></html>",
            encoding="utf-8",
        )
        config["plugins"]["htmlrender"]["resources"] = {"local_access": {"allowed_paths": [str(template_root)]}}
        shutil.copyfile(
            workspace / "tests/fixtures/startup_resource_probe.py",
            workspace / "plugins/startup_resource_probe.py",
        )
        config["plugins"].setdefault("$prefix", []).append({"key": "", "plugins": ["startup_resource_probe"]})
        config["plugins"]["startup_resource_probe"] = {}
    local = workspace / "entari.local.yml"
    write_config(local, config)
    original = local.read_bytes()
    run(workspace, env, "run", "--locked", "main.py", "--prepare")
    run(workspace, env, "run", "--locked", "main.py", "--check")
    serve_once(workspace, env, port, resources=resources)
    database = workspace / "persistent/chat.db"
    if not database.parent.is_dir():
        raise SmokeFailure("Startup ignored the selected local database directory")
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE startup_smoke (value TEXT NOT NULL)")
        connection.execute("INSERT INTO startup_smoke VALUES ('retained')")
    connection.close()
    run(workspace, env, "sync", "--locked", "--all-extras")
    run(workspace, env, "run", "--locked", "main.py", "--check")
    serve_once(workspace, env, port, resources=resources)
    with sqlite3.connect(database) as connection:
        if connection.execute("SELECT value FROM startup_smoke").fetchall() != [("retained",)]:
            raise SmokeFailure("Upgrade lost existing database state")
    connection.close()
    if local.read_bytes() != original:
        raise SmokeFailure("Startup or upgrade rewrote personal configuration")
    config["plugins"]["database"]["name"] = "rejected/chat.db"
    config["plugins"]["plugin_workshop"] = {}
    config["plugins"]["entari_plugin_webui"] = {"password": ""}
    write_config(local, config)
    run(workspace, env, "run", "--locked", "main.py", success=False)
    if (workspace / "rejected").exists():
        raise SmokeFailure("Invalid configuration caused storage writes")
    if resources:
        code = """from pathlib import Path
import sys
from utils.startup.resources import configure_resources, prepare_resources, resource_issues
plugins = {'.localdata': {'app_name': 'chtholly'}, 'llm': {}}
configure_resources(plugins, Path.cwd())
prepare_resources(plugins, Path.cwd())
assert not resource_issues(plugins, Path.cwd())
attempts = []
def deny_network(event, args):
    if event in ('socket.connect', 'socket.getaddrinfo'):
        attempts.append(event)
        raise RuntimeError('Offline tokenizer verification')
sys.addaudithook(deny_network)
import litellm
import tiktoken
for name in ('cl100k_base', 'o200k_base'):
    assert tiktoken.get_encoding(name).encode('startup smoke')
assert not attempts
"""
        run(workspace, env, "run", "--locked", "python", "-c", code)
    return {
        "fresh_install": True,
        "native_api": True,
        "local_config_selected": True,
        "database_preserved": True,
        "invalid_config_rejected_before_writes": True,
        "resources": resources,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resources", action="store_true", help="also prepare real browsers and tokenizers")
    args = parser.parse_args()
    sys.stdout.write("Running isolated startup smoke.\n")
    sys.stdout.flush()
    try:
        with tempfile.TemporaryDirectory(prefix="chtholly-startup-smoke-") as directory:
            result = exercise(Path(directory), resources=args.resources)
        sys.stdout.write(json.dumps(result) + "\n")
    except (OSError, SmokeFailure, subprocess.SubprocessError) as exc:
        sys.stderr.write(f"Startup smoke failed: {type(exc).__name__}: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
