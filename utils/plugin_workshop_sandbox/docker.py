"""Host-side Docker boundary. Worker claims are untrusted functional evidence only."""

from __future__ import annotations

import re
import json
import math
import uuid
import asyncio
from pathlib import Path
from contextlib import AsyncExitStack
from dataclasses import asdict
from importlib.metadata import PackageNotFoundError, version

from utils.plugin_workshop_core.models import CheckResult, SandboxRequest, ValidationReport

from .egress import WorkshopEgress
from .process import ProcessIOError, settle, run_process

PROTOCOL = "1"
WARNING = (
    "Untrusted functional acceptance, not a security certification. Candidate code shares the worker interpreter "
    "and can forge its claims. Docker isolates the host; native activation requires human hash-bound approval."
)
STAGES = frozenset(
    {
        "syntax",
        "render_capability",
        "import",
        "commands",
        "execution",
        "reload",
        "failed_reload",
        "cancellation",
        "services",
        "unload",
    }
)


class DockerSandbox:
    def __init__(
        self,
        image: str = "chtholly-workshop:local",
        *,
        egress_root: Path,
        executable: str = "docker",
        framework_version: str | None = None,
    ):
        self.image = image
        self.executable = executable
        self.egress_root = Path(egress_root)
        if framework_version is None:
            try:
                framework_version = version("arclet-entari")
            except PackageNotFoundError:
                framework_version = ""
        self.framework_version = framework_version
        self._validations: set[asyncio.Task] = set()
        self._containers: set[str] = set()
        self._closed = False
        self._close_task: asyncio.Task | None = None

    async def status(self) -> dict[str, object]:
        result: dict[str, object] = {
            "available": False,
            "backend": "docker",
            "image_id": "",
            "reason": "",
            "warning": WARNING,
        }
        if self._closed:
            result["reason"] = "Sandbox is closed"
            return result
        try:
            if not self.framework_version:
                raise RuntimeError("Host Entari distribution provenance is unavailable")
            server = await run_process(self.executable, "version", "--format", "{{.Server.Os}}", output_bytes=8192)
            if server.code or server.stdout.strip() != b"linux":
                raise RuntimeError("A reachable Linux Docker engine is required")
            inspected = await run_process(self.executable, "image", "inspect", self.image, output_bytes=65536)
            if inspected.code:
                raise RuntimeError("Sandbox image is unavailable; an operator must run the explicit image builder")
            image = json.loads(inspected.stdout)[0]
            image_id = image["Id"]
            if not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id) or image.get("Os") != "linux":
                raise RuntimeError("Sandbox image must be a Linux image with immutable SHA256 identity")
            labels = image.get("Config", {}).get("Labels") or {}
            if image.get("Config", {}).get("Volumes"):
                raise RuntimeError("Sandbox images declaring volumes are forbidden")
            if labels.get("org.chtholly.workshop.protocol") != PROTOCOL:
                raise RuntimeError("Sandbox image protocol does not match this worker")
            if labels.get("org.chtholly.workshop.framework") != self.framework_version:
                raise RuntimeError("Sandbox image Entari version does not match the host")
            if labels.get("org.chtholly.workshop.python") != "3.10":
                raise RuntimeError("Sandbox image must use Python 3.10")
            if labels.get("org.chtholly.workshop.rendering") != "playwright-v1":
                raise RuntimeError("Sandbox image lacks the trusted Playwright rendering capability; rebuild it")
            if labels.get("org.chtholly.workshop.egress") != "open-meteo-v1":
                raise RuntimeError("Sandbox image lacks the bounded Open-Meteo transport; rebuild it")
            result["capabilities"] = {
                "rendering": "playwright",
                "network": "none",
                "controlled_https": ["geocoding-api.open-meteo.com/v1/search", "api.open-meteo.com/v1/forecast"],
            }
            result.update(available=True, image_id=image_id)
        except (OSError, ValueError, KeyError, IndexError, TypeError, RuntimeError, asyncio.TimeoutError) as exc:
            result["reason"] = str(exc)[:1000]
        return result

    def _report(
        self,
        request: SandboxRequest,
        status: str,
        message: str,
        image_id: str = "",
        *,
        log: str = "",
    ) -> ValidationReport:
        return ValidationReport(
            status,
            request.source_hash,
            (CheckResult("backend", False, message),),
            (WARNING + "\n" + log)[:65536],
            self.framework_version,
            "",
            image_id,
        )

    async def _remove(self, name: str) -> None:
        removed = await run_process(self.executable, "container", "rm", "--force", name, timeout=15, output_bytes=8192)
        if removed.code and b"No such container" not in removed.stderr:
            raise RuntimeError("Docker could not remove the owned sandbox container")
        self._containers.discard(name)

    async def validate(self, request: SandboxRequest) -> ValidationReport:
        if self._closed:
            return self._report(request, "unavailable", "Sandbox is closed")
        current = asyncio.current_task()
        assert current is not None
        self._validations.add(current)
        name = "chtholly-workshop-" + uuid.uuid4().hex
        image_id = ""
        resources = AsyncExitStack()
        try:
            from utils.plugin_workshop_core.codec import manifest_payload
            from utils.plugin_workshop_core.policy import parse_manifest, normalize_submission

            # Validate the dataclass too: callers cannot bypass the public upload boundary.
            manifest = parse_manifest(manifest_payload(request.manifest))
            _, files, digest = normalize_submission(request.plugin_name, request.files, manifest)
            if digest != request.source_hash:
                return self._report(request, "failed", "Submission digest does not match its immutable content")
            limits = request.limits
            if not (
                math.isfinite(limits.timeout_seconds)
                and 1 <= limits.timeout_seconds <= 300
                and 128 <= limits.memory_mb <= 2048
                and math.isfinite(limits.cpus)
                and 0.1 <= limits.cpus <= 4
                and 16 <= limits.pids <= 256
                and 4096 <= limits.output_bytes <= 1024 * 1024
            ):
                return self._report(request, "failed", "Sandbox resource limits are outside safe bounds")
            readiness = await self.status()
            if not readiness["available"]:
                return self._report(request, "unavailable", str(readiness["reason"]))
            image_id = str(readiness["image_id"])
            payload = (
                json.dumps(
                    {
                        "protocol": PROTOCOL,
                        "plugin_name": request.plugin_name,
                        "source_hash": digest,
                        "files": files,
                        "manifest": asdict(manifest),
                        "occupied_commands": request.occupied_commands,
                        "framework_version": self.framework_version,
                    },
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode("utf-8")
                + b"\n"
            )
            if len(payload) > 2 * 1024 * 1024:
                return self._report(request, "failed", "Worker request exceeds its wire limit", image_id)
            egress = await resources.enter_async_context(WorkshopEgress(self.egress_root))
            self._containers.add(name)
            created = await run_process(
                self.executable,
                "container",
                "create",
                "--name",
                name,
                "--pull=never",
                "--interactive",
                "--label",
                "org.chtholly.workshop.owned=1",
                "--network",
                "none",
                "--read-only",
                "--mount",
                f"type=bind,src={egress.socket_path},dst=/run/workshop-http.sock,readonly",
                "--user",
                "65532:65532",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--memory",
                f"{limits.memory_mb}m",
                "--memory-swap",
                f"{limits.memory_mb}m",
                "--cpus",
                str(limits.cpus),
                "--pids-limit",
                str(limits.pids),
                "--ulimit",
                "nofile=256:256",
                "--ulimit",
                "core=0:0",
                "--shm-size",
                "128m",
                "--log-driver",
                "none",
                "--ipc",
                "private",
                "--workdir",
                "/workspace",
                "--tmpfs",
                "/workspace:rw,nosuid,nodev,noexec,size=64m,uid=65532,gid=65532,mode=700",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,noexec,size=256m,uid=65532,gid=65532,mode=700",
                "--env",
                "HOME=/workspace",
                "--env",
                "TMPDIR=/tmp",
                "--env",
                "PYTHONDONTWRITEBYTECODE=1",
                "--env",
                "PYTHONUNBUFFERED=1",
                "--env",
                "PYTHONIOENCODING=utf-8",
                "--entrypoint",
                "/opt/workshop/.venv/bin/python",
                image_id,
                "-I",
                "-B",
                "/opt/workshop/worker/worker.py",
                output_bytes=8192,
            )
            if created.code:
                return self._report(request, "unavailable", "Docker rejected the isolation configuration", image_id)
            completed = await run_process(
                self.executable,
                "container",
                "start",
                "--attach",
                "--interactive",
                name,
                input_bytes=payload,
                timeout=limits.timeout_seconds,
                output_bytes=limits.output_bytes,
            )
            if completed.code:
                return self._report(
                    request,
                    "failed",
                    f"Worker exited with code {completed.code}",
                    image_id,
                    log=completed.stderr.decode("utf-8", errors="replace"),
                )
            return self._decode(request, completed.stdout, completed.stderr, image_id)
        except ProcessIOError as exc:
            return self._report(
                request,
                "failed" if image_id else "unavailable",
                str(exc),
                image_id,
                log=exc.stderr.decode("utf-8", errors="replace"),
            )
        except asyncio.TimeoutError:
            return self._report(request, "failed", "Sandbox exceeded its time limit", image_id)
        except (OSError, ValueError, RuntimeError) as exc:
            return self._report(request, "failed" if image_id else "unavailable", str(exc)[:1000], image_id)
        finally:
            try:
                if name in self._containers:
                    await settle(asyncio.create_task(self._remove(name)))
            finally:
                try:
                    await settle(asyncio.create_task(resources.aclose()))
                finally:
                    self._validations.discard(current)

    def _decode(self, request: SandboxRequest, stdout: bytes, stderr: bytes, image_id: str) -> ValidationReport:
        payload = json.loads(stdout)
        if not isinstance(payload, dict) or payload.get("protocol") != PROTOCOL:
            raise ValueError("Malformed worker protocol")
        if payload.get("source_hash") != request.source_hash:
            raise ValueError("Worker returned a different submission digest")
        python_version = payload.get("python_version", "")
        # Worker output is hostile input even when the container exited cleanly.
        if (
            payload.get("framework_version") != self.framework_version
            or not isinstance(python_version, str)
            or not python_version.startswith("3.10.")
        ):
            raise ValueError("Worker interpreter/framework provenance differs from the approved image contract")
        checks = payload.get("checks")
        if not isinstance(checks, list) or not 1 <= len(checks) <= 128:
            raise ValueError("Worker returned no bounded acceptance checks")
        results = []
        for item in checks:
            if not isinstance(item, dict) or type(item.get("passed")) is not bool:
                raise ValueError("Worker returned a malformed check")
            if not isinstance(item.get("name"), str) or not isinstance(item.get("detail", ""), str):
                raise ValueError("Worker returned malformed check text")
            results.append(CheckResult(item["name"][:100], item["passed"], item.get("detail", "")[:4000]))
        names = {item.name for item in results}
        if not STAGES <= names:
            raise ValueError("Worker omitted required lifecycle acceptance stages")
        passed = all(item.passed for item in results) and payload.get("status") == "passed"
        return ValidationReport(
            "passed" if passed else "failed",
            request.source_hash,
            tuple(results),
            (WARNING + "\n" + stderr.decode("utf-8", errors="replace"))[:65536],
            self.framework_version,
            python_version,
            image_id,
        )

    async def aclose(self) -> None:
        self._closed = True

        async def close() -> None:
            tasks = tuple(self._validations)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            errors = await asyncio.gather(
                *(self._remove(name) for name in tuple(self._containers)), return_exceptions=True
            )
            if any(isinstance(error, BaseException) for error in errors):
                raise RuntimeError("One or more owned sandbox containers could not be removed")

        if self._close_task is None:
            self._close_task = asyncio.create_task(close())
        await settle(self._close_task)
