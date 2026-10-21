"""Durable workshop lifecycle boundaries with controlled sandbox/native drivers."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace

import pytest
import pytest_asyncio

from plugins.plugin_workshop.config import WorkshopConfig
from plugins.plugin_workshop.runtime import NativePluginDriver
from plugins.plugin_workshop.service import PluginWorkshopService
from utils.plugin_workshop_core.models import Actor, CheckResult, WorkshopError, ValidationReport

SUPERUSER = Actor("bot-superuser", scope_id=1, is_admin=True, is_superuser=True)
ADMIN = Actor("authenticated-webui", scope_id=1, is_admin=True)
SOURCE = "VALUE = 1\n"


def declaration():
    return {
        "title": "Counter",
        "description": "A managed command",
        "commands": ["workshop_ping"],
        "permissions": [],
        "data_description": "No persistent data",
        "checks": [{"command": "workshop_ping", "expected_contains": "pong"}],
    }


class ControlledSandbox:
    def __init__(self):
        self.framework_version = ""
        self.requests = []
        self.passed = True
        self.started = asyncio.Event()
        self.release = None
        self.closed = False

    async def status(self):
        return {"available": True}

    async def validate(self, request):
        self.requests.append(request)
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        return ValidationReport(
            "passed" if self.passed else "failed",
            request.source_hash,
            (CheckResult("execution", self.passed, "Observed pong" if self.passed else "Missing pong"),),
            framework_version=self.framework_version,
            python_version="3.11.15",
            image_id="sha256:" + "a" * 64,
        )

    async def aclose(self):
        self.closed = True


class ControlledDriver:
    def __init__(self):
        self.loaded = {}
        self.calls = []
        self.fail_apply = {}
        self.apply_gate = None
        self.disable_gate = None
        self.fail_disable = False
        self.closed = False

    def prepare(self):
        return None

    def reconcile(self):
        return None

    def occupied_commands(self, _name):
        return ()

    def is_loaded(self, name, version=None):
        record = self.loaded.get(name)
        return record is not None and (version is None or record.version == version)

    async def apply(self, record, _files):
        self.calls.append(("apply", record.version))
        if self.apply_gate is not None and self.apply_gate[0] == record.version:
            _, entered, release = self.apply_gate
            entered.set()
            await release.wait()
        if self.fail_apply.get(record.version) == "missing":
            self.loaded.pop(record.plugin_name, None)
            raise RuntimeError("Replacement unloaded the prior native plugin")
        self.loaded[record.plugin_name] = record
        if self.fail_apply.get(record.version) == "candidate":
            raise RuntimeError("Candidate remained loaded after native rejection")

    async def disable(self, name):
        self.calls.append(("disable", None))
        if self.disable_gate is not None:
            entered, release = self.disable_gate
            entered.set()
            await release.wait()
        self.loaded.pop(name, None)
        if self.fail_disable:
            raise RuntimeError("Disable failed after native unload")

    async def aclose(self):
        self.loaded.clear()
        self.closed = True


def make_service(root):
    sandbox, driver = ControlledSandbox(), ControlledDriver()
    service = PluginWorkshopService(WorkshopConfig(), root=root, sandbox=sandbox, driver=driver)
    sandbox.framework_version = service.framework_version
    return service


@pytest_asyncio.fixture
async def service(tmp_path):
    result = make_service(tmp_path)
    await result.prepare()
    await result.restore()
    try:
        yield result
    finally:
        await result.aclose()


async def submit(service, *, value=1, on_created=None):
    return await service.submit(
        "counter", {"__init__.py": f"VALUE = {value}\n"}, declaration(), SUPERUSER, on_created=on_created
    )


def audit(service):
    with sqlite3.connect(service.root / "workshop.sqlite3") as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute("SELECT * FROM operations ORDER BY created_at,operation_id")]


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", [Actor("ordinary", scope_id=1), ADMIN, Actor("forged", is_superuser=1), None])
async def test_service_mutations_reject_before_readiness_storage_or_sandbox(tmp_path, actor):
    root = tmp_path / "untouched"
    service = make_service(root)
    changed, created = [], []
    try:
        for mutation in (
            lambda: service.submit("counter", {}, {}, actor, on_created=created.append),
            lambda: service.validate("counter", 1, actor),
            lambda: service.activate("counter", 1, "a" * 64, actor, on_changed=changed.append),
            lambda: service.rollback("counter", 1, "a" * 64, actor, on_changed=changed.append),
            lambda: service.disable("counter", actor, on_changed=changed.append),
        ):
            with pytest.raises(WorkshopError) as denied:
                await mutation()
            assert denied.value.code == "forbidden"
            assert denied.value.status == 403
        assert not root.exists()
        assert not created
        assert not changed
        assert service._admitted == 0
        assert service._writes == service._changes == set()
        assert not service.sandbox.requests
        assert not service.driver.calls
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_passed_candidate_restart_stays_inactive_until_separate_activation(tmp_path):
    original = make_service(tmp_path)
    try:
        await original.prepare()
        await original.restore()
        saved = await submit(original)
        assert saved.report.passed
        assert not saved.active
        assert not saved.ever_active
        assert original.driver.calls == []
        assert audit(original) == []
    finally:
        await original.aclose()
    restarted = make_service(tmp_path)
    try:
        await restarted.prepare()
        await restarted.restore()
        candidate = await restarted.detail("counter", saved.version, ADMIN)
        assert candidate.report.passed
        assert not candidate.active
        assert not candidate.ever_active
        assert restarted.driver.calls == []
        assert restarted.store.active_versions() == []
        changed = []
        state = await restarted.activate(
            "counter", saved.version, saved.source_hash, SUPERUSER, on_changed=changed.append
        )
        assert state.enabled
        assert state.active_version == saved.version
        assert changed == [state]
        assert restarted.driver.is_loaded("counter", saved.version)
        operation = audit(restarted)[0]
        assert operation["action"] == "activate"
        assert operation["status"] == "succeeded"
        assert operation["actor_key"] == SUPERUSER.key
        assert operation["source_hash"] == saved.source_hash
    finally:
        await restarted.aclose()


@pytest.mark.asyncio
async def test_restart_settles_interrupted_change_and_restores_only_committed_version(tmp_path):
    original = make_service(tmp_path)
    try:
        await original.prepare()
        await original.restore()
        first, second = await submit(original), await submit(original, value=2)
        await original.activate("counter", first.version, first.source_hash, SUPERUSER)
        pending = original.store.begin_change(
            "counter", second.version, second.source_hash, SUPERUSER, action="activate"
        )
    finally:
        await original.aclose()
    restarted = make_service(tmp_path)
    try:
        await restarted.prepare()
        await restarted.restore()
        assert restarted.driver.calls == [("apply", first.version)]
        assert restarted.driver.is_loaded("counter", first.version)
        assert not (await restarted.detail("counter", second.version, ADMIN)).ever_active
        assert restarted.store.pending_operations() == []
        operation = next(row for row in audit(restarted) if row["operation_id"] == pending.operation_id)
        assert operation["status"] == "failed"
        assert operation["actor_key"] == SUPERUSER.key
        assert restarted.store.active_versions()[0].version == first.version
    finally:
        await restarted.aclose()


@pytest.mark.asyncio
async def test_failed_acceptance_stays_inactive_and_revised_version_can_activate(service):
    service.sandbox.passed = False
    failed = await submit(service)
    assert failed.validation_status == "failed"
    assert not failed.report.passed
    with pytest.raises(WorkshopError) as rejected:
        await service.activate("counter", failed.version, failed.source_hash, SUPERUSER)
    assert rejected.value.code == "validation_required"
    assert service.driver.calls == []
    service.sandbox.passed = True
    corrected = await submit(service, value=2)
    assert corrected.version > failed.version
    assert corrected.source_hash != failed.source_hash
    assert corrected.report.passed
    assert service.driver.calls == []
    state = await service.activate("counter", corrected.version, corrected.source_hash, SUPERUSER)
    assert state.enabled
    assert state.active_version == corrected.version
    assert not (await service.detail("counter", failed.version, ADMIN)).ever_active


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["candidate", "missing"])
async def test_failed_replacement_restores_prior_version_and_preserves_rollback_history(service, failure):
    first, second = await submit(service), await submit(service, value=2)
    await service.activate("counter", first.version, first.source_hash, SUPERUSER)
    service.driver.fail_apply[second.version] = failure
    changed = []
    with pytest.raises(WorkshopError) as rejected:
        await service.activate("counter", second.version, second.source_hash, SUPERUSER, on_changed=changed.append)
    assert rejected.value.code == "native_change_failed"
    assert changed == []
    assert service.driver.calls == [("apply", first.version), ("apply", second.version), ("apply", first.version)]
    assert service.driver.is_loaded("counter", first.version)
    assert service.store.active_versions()[0].version == first.version
    assert not (await service.detail("counter", second.version, ADMIN)).ever_active
    assert audit(service)[-1]["status"] == "failed"
    service.driver.fail_apply.clear()
    await service.activate("counter", second.version, second.source_hash, SUPERUSER)
    assert (await service.detail("counter", second.version, ADMIN)).ever_active
    rolled_back = await service.rollback("counter", first.version, first.source_hash, SUPERUSER)
    assert rolled_back.enabled
    assert rolled_back.active_version == first.version
    calls = list(service.driver.calls)
    repeated = await service.activate("counter", first.version, first.source_hash, SUPERUSER)
    assert repeated == rolled_back
    assert service.driver.calls == calls


@pytest.mark.asyncio
async def test_failed_durable_commit_compensates_runtime_and_records_failure(service, monkeypatch):
    first, second = await submit(service), await submit(service, value=2)
    await service.activate("counter", first.version, first.source_hash, SUPERUSER)
    complete_change = service.store.complete_change

    def reject_success(operation_id, *, success, error=""):
        if success:
            raise OSError("Durable commit rejected")
        return complete_change(operation_id, success=success, error=error)

    monkeypatch.setattr(service.store, "complete_change", reject_success)
    changed = []
    with pytest.raises(WorkshopError) as rejected:
        await service.activate("counter", second.version, second.source_hash, SUPERUSER, on_changed=changed.append)
    assert rejected.value.code == "native_change_failed"
    assert changed == []
    assert service.driver.is_loaded("counter", first.version)
    assert service.store.active_versions()[0].version == first.version
    assert not (await service.detail("counter", second.version, ADMIN)).ever_active
    assert audit(service)[-1]["status"] == "failed"


@pytest.mark.asyncio
async def test_disable_failure_restores_committed_runtime_without_success_callback(service):
    first = await submit(service)
    await service.activate("counter", first.version, first.source_hash, SUPERUSER)
    service.driver.fail_disable = True
    changed = []
    with pytest.raises(WorkshopError) as rejected:
        await service.disable("counter", SUPERUSER, on_changed=changed.append)
    assert rejected.value.code == "native_change_failed"
    assert changed == []
    assert service.driver.is_loaded("counter", first.version)
    assert service.store.active_versions()[0].version == first.version
    assert audit(service)[-1]["action"] == "disable"
    assert audit(service)[-1]["status"] == "failed"


@pytest.mark.asyncio
async def test_native_changes_are_serialized_until_commit_and_callbacks(service):
    first, second = await submit(service), await submit(service, value=2)
    await service.activate("counter", first.version, first.source_hash, SUPERUSER)
    entered, release = asyncio.Event(), asyncio.Event()
    service.driver.apply_gate = (second.version, entered, release)
    changed = []
    activation = asyncio.create_task(
        service.activate("counter", second.version, second.source_hash, SUPERUSER, on_changed=changed.append)
    )
    await entered.wait()
    disabling = asyncio.create_task(service.disable("counter", SUPERUSER, on_changed=changed.append))
    await asyncio.sleep(0)
    assert service.store.active_versions()[0].version == first.version
    assert ("disable", None) not in service.driver.calls
    release.set()
    activated, disabled = await asyncio.gather(activation, disabling)
    assert activated.enabled
    assert activated.active_version == second.version
    assert not disabled.enabled
    assert disabled.active_version == second.version
    assert changed == [activated, disabled]
    assert service.store.active_versions() == []
    assert [row["action"] for row in audit(service)] == ["activate", "activate", "disable"]
    assert all(row["status"] == "succeeded" for row in audit(service))


@pytest.mark.asyncio
async def test_cancelled_disable_settles_actual_commit_and_callback_once(service):
    first = await submit(service)
    await service.activate("counter", first.version, first.source_hash, SUPERUSER)
    entered, release = asyncio.Event(), asyncio.Event()
    service.driver.disable_gate = (entered, release)
    changed = []
    task = asyncio.create_task(service.disable("counter", SUPERUSER, on_changed=changed.append))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(changed) == 1
    assert not changed[0].enabled
    assert changed[0].active_version == first.version
    assert service.store.active_versions() == []
    assert not service.driver.is_loaded("counter")
    assert audit(service)[-1]["action"] == "disable"
    assert audit(service)[-1]["status"] == "succeeded"
    assert service._changes == set()


@pytest.mark.asyncio
async def test_cancelled_validation_keeps_created_candidate_and_terminal_report(service):
    service.sandbox.release = asyncio.Event()
    created = []
    task = asyncio.create_task(submit(service, on_created=created.append))
    await service.sandbox.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(created) == 1
    record = await service.detail("counter", created[0].version, ADMIN)
    assert record.validation_status == "cancelled"
    assert not record.report.passed
    assert not record.active
    assert not record.ever_active
    assert await service.source("counter", record.version, ADMIN) == {"__init__.py": SOURCE}
    assert service.store.active_versions() == []
    assert service.driver.calls == []
    assert service._admitted == 0
    assert not service._validations


@pytest.mark.asyncio
async def test_stale_hash_and_never_active_rollback_reject_before_native_effects(service):
    first, second = await submit(service), await submit(service, value=2)
    with pytest.raises(WorkshopError) as stale:
        await service.activate("counter", second.version, first.source_hash, SUPERUSER)
    assert stale.value.code == "stale_hash"
    with pytest.raises(WorkshopError) as unavailable:
        await service.rollback("counter", second.version, second.source_hash, SUPERUSER)
    assert unavailable.value.code == "rollback_unavailable"
    assert service.driver.calls == []
    assert service.store.active_versions() == []
    assert all(row["status"] == "failed" for row in audit(service))


@pytest.mark.asyncio
async def test_native_publication_requires_successful_exact_report_and_immutable_hash(service, tmp_path):
    record = await submit(service)
    root = tmp_path / "unprepared-native-runtime"
    driver = NativePluginDriver(root)
    for invalid in (
        replace(record, report=None),
        replace(record, validation_status="failed"),
        replace(record, report=replace(record.report, source_hash="f" * 64)),
    ):
        with pytest.raises(WorkshopError) as rejected:
            await driver.apply(invalid, {"__init__.py": SOURCE})
        assert rejected.value.code == "acceptance_required"
        assert not root.exists()
    with pytest.raises(WorkshopError) as changed:
        await driver.apply(record, {"__init__.py": "VALUE = 2\n"})
    assert changed.value.code == "source_mismatch"
    assert not root.exists()
