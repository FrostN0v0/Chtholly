"""Python 3.11-only sandbox provenance and trusted build-input contracts."""

from __future__ import annotations

import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from zipfile import ZipFile

import pytest

from utils.plugin_workshop_sandbox import docker as sandbox_docker
from scripts.build_workshop_sandbox import WORKER_FILES, collect_inputs
from utils.plugin_workshop_core.store import WorkshopStore
from utils.plugin_workshop_core.models import Actor, CheckResult, WorkshopError, SandboxRequest, ValidationReport
from utils.plugin_workshop_core.policy import parse_manifest
from utils.plugin_workshop_sandbox.process import ProcessResult

FRAMEWORK = "0.19.0+chtholly.1"
IMAGE_ID = "sha256:" + "a" * 64


def declaration():
    return parse_manifest(
        {
            "title": "Counter",
            "description": "A small managed command",
            "commands": ["workshop_ping"],
            "permissions": [],
            "data_description": "No persistent data",
            "configuration": {},
            "checks": [{"command": "workshop_ping", "expected_contains": "pong"}],
        }
    )


@pytest.mark.parametrize("python_version", ["3.10.20", "3.11.14", "3.12.0"])
def test_worker_report_requires_python_311(tmp_path, python_version):
    sandbox = sandbox_docker.DockerSandbox(egress_root=tmp_path, framework_version=FRAMEWORK)
    request = SandboxRequest("counter", "b" * 64, {"__init__.py": ""}, declaration())
    payload = {
        "protocol": sandbox_docker.PROTOCOL,
        "source_hash": request.source_hash,
        "framework_version": FRAMEWORK,
        "python_version": python_version,
        "checks": [{"name": name, "passed": True} for name in sorted(sandbox_docker.STAGES)],
        "status": "passed",
    }
    raw = json.dumps(payload).encode()
    if python_version.startswith("3.11."):
        report = sandbox._decode(request, raw, b"", IMAGE_ID)
        assert report.passed
        assert report.python_version == python_version
        assert report.framework_version == FRAMEWORK
        assert report.image_id == IMAGE_ID
    else:
        with pytest.raises(ValueError, match="provenance"):
            sandbox._decode(request, raw, b"", IMAGE_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize("python_version", ["3.10", "3.11", "3.12"])
async def test_image_label_requires_python_311(tmp_path, monkeypatch, python_version):
    labels = {
        "org.chtholly.workshop.protocol": sandbox_docker.PROTOCOL,
        "org.chtholly.workshop.framework": FRAMEWORK,
        "org.chtholly.workshop.python": python_version,
        "org.chtholly.workshop.rendering": "playwright-v1",
        "org.chtholly.workshop.egress": "open-meteo-v1",
    }

    async def run_process(executable, *args, **kwargs):
        if args[0] == "version":
            return ProcessResult(0, b"linux\n", b"")
        assert args == ("image", "inspect", "chtholly-workshop:local")
        image = {"Id": IMAGE_ID, "Os": "linux", "Config": {"Labels": labels}}
        return ProcessResult(0, json.dumps([image]).encode(), b"")

    monkeypatch.setattr(sandbox_docker, "run_process", run_process)
    sandbox = sandbox_docker.DockerSandbox(egress_root=tmp_path, framework_version=FRAMEWORK)
    status = await sandbox.status()
    assert status["available"] is (python_version == "3.11")


@pytest.mark.parametrize("python_version", ["3.10.20", "3.11.14", "3.12.0"])
def test_native_activation_requires_python_311_report(tmp_path, python_version):
    from plugins.plugin_workshop.config import WorkshopConfig
    from plugins.plugin_workshop.service import PluginWorkshopService

    store = WorkshopStore(tmp_path)
    store.initialize()
    record = store.create_version(
        "counter", {"__init__.py": "VALUE = 1\n"}, declaration(), Actor("owner", 1, is_superuser=True)
    )
    service = PluginWorkshopService(WorkshopConfig(), root=tmp_path, store=store)
    store.mark_validating(record.plugin_name, record.version)
    record = store.record_report(
        record.plugin_name,
        record.version,
        ValidationReport(
            "passed",
            record.source_hash,
            (CheckResult("execution", True),),
            framework_version=service.framework_version,
            python_version=python_version,
            image_id=IMAGE_ID,
        ),
    )
    if python_version.startswith("3.11."):
        service._accepted(record, record.source_hash)
    else:
        with pytest.raises(WorkshopError) as error:
            service._accepted(record, record.source_hash)
        assert error.value.code == "report_mismatch"


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"framework_version": "obsolete-framework"}, "framework_mismatch"),
        ({"image_id": "local:latest"}, "report_mismatch"),
        ({"image_id": "sha256:not-an-immutable-image-id"}, "report_mismatch"),
        ({"source_hash": "f" * 64}, "acceptance_required"),
        ({"status": "failed"}, "acceptance_required"),
        ({"checks": ()}, "acceptance_required"),
    ],
)
def test_native_activation_requires_current_framework_exact_hash_and_pinned_report(tmp_path, changes, code):
    from plugins.plugin_workshop.config import WorkshopConfig
    from plugins.plugin_workshop.service import PluginWorkshopService

    store = WorkshopStore(tmp_path)
    store.initialize()
    record = store.create_version(
        "counter", {"__init__.py": "VALUE = 1\n"}, declaration(), Actor("owner", 1, is_superuser=True)
    )
    service = PluginWorkshopService(WorkshopConfig(), root=tmp_path, store=store)
    report = ValidationReport(
        "passed",
        record.source_hash,
        (CheckResult("execution", True),),
        framework_version=service.framework_version,
        python_version="3.11.15",
        image_id=IMAGE_ID,
    )
    record = replace(record, validation_status="passed", report=replace(report, **changes))
    with pytest.raises(WorkshopError) as rejected:
        service._accepted(record, record.source_hash)
    assert rejected.value.code == code


def build_inputs(root: Path, *, with_llm: bool) -> dict[str, bytes]:
    wheels = {
        "entari": f"vendor/entari/arclet_entari-{FRAMEWORK}-py3-none-any.whl",
        "webui": "vendor/webui/entari_plugin_webui-1.0.3+chtholly.5-py3-none-any.whl",
        "htmlrender": "vendor/htmlrender/entari_plugin_htmlrender-0.1.0+chtholly.1-py3-none-any.whl",
        "satori": "vendor/satori/satori_python-1.3.9.post1+chtholly.1-py3-none-any.whl",
        "satori-server": "vendor/satori/satori_python_server-1.3.7+chtholly.1-py3-none-any.whl",
    }
    if with_llm:
        wheels["llm"] = "vendor/llm/entari_plugin_llm-0.2.2+chtholly.2-py3-none-any.whl"
    for wheel in wheels.values():
        target = root / wheel
        target.parent.mkdir(parents=True, exist_ok=True)
        with ZipFile(target, "w") as archive:
            archive.writestr("arclet_entari.dist-info/METADATA", f"Name: arclet-entari\nVersion: {FRAMEWORK}\n")
    paths = "\n".join(f'path = "{wheel}"' for wheel in wheels.values()).encode()
    expected = {"pyproject.toml": paths, "uv.lock": paths}
    expected.update({wheel: (root / wheel).read_bytes() for wheel in wheels.values()})
    dockerfile = root / "scripts/workshop_sandbox/Dockerfile"
    dockerfile.parent.mkdir(parents=True)
    dockerfile.write_bytes(b"FROM trusted-base\n")
    expected["Dockerfile"] = dockerfile.read_bytes()
    for name in WORKER_FILES:
        worker = root / "utils/plugin_workshop_sandbox" / name
        worker.parent.mkdir(parents=True, exist_ok=True)
        worker.write_bytes(b"# trusted worker input\n")
        expected[f"worker/{name}"] = worker.read_bytes()
    for name in ("pyproject.toml", "uv.lock"):
        (root / name).write_bytes(expected[name])
    (root / ".env").write_bytes(b"SECRET=not-a-build-input\n")
    return expected


def test_builder_collects_exact_vendored_inputs_and_derives_framework(tmp_path):
    expected = build_inputs(tmp_path, with_llm=True)
    inputs, framework, wheel_hash = collect_inputs(tmp_path)
    assert inputs == expected
    assert framework == FRAMEWORK
    assert wheel_hash == sha256(expected[f"vendor/entari/arclet_entari-{FRAMEWORK}-py3-none-any.whl"]).hexdigest()


def test_builder_rejects_llm_source_missing_from_lock(tmp_path):
    build_inputs(tmp_path, with_llm=True)
    lock = tmp_path / "uv.lock"
    lock.write_text(
        "\n".join(line for line in lock.read_text(encoding="utf-8").splitlines() if "vendor/llm/" not in line),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="exactly one vendored entari_plugin_llm wheel"):
        collect_inputs(tmp_path)


def test_builder_rejects_missing_vendored_llm(tmp_path):
    build_inputs(tmp_path, with_llm=False)
    with pytest.raises(ValueError, match="exactly one vendored entari_plugin_llm wheel"):
        collect_inputs(tmp_path)


@pytest.mark.parametrize("distribution", ["satori_python", "satori_python_server"])
@pytest.mark.parametrize("missing_from", ["pyproject.toml", "uv.lock", "wheel"])
def test_builder_requires_both_patched_satori_wheels(tmp_path, distribution, missing_from):
    expected = build_inputs(tmp_path, with_llm=True)
    wheel_path = next(path for path in expected if path.startswith(f"vendor/satori/{distribution}-"))
    if missing_from == "wheel":
        (tmp_path / wheel_path).unlink()
    else:
        manifest = tmp_path / missing_from
        manifest.write_text(
            "\n".join(line for line in manifest.read_text(encoding="utf-8").splitlines() if wheel_path not in line),
            encoding="utf-8",
        )
    with pytest.raises(ValueError, match=distribution):
        collect_inputs(tmp_path)
