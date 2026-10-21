"""Superuser mutations, immutable bytes, acceptance and crash-recovery contracts."""

from __future__ import annotations

import sqlite3

import pytest

from utils.plugin_workshop_core.codec import version_payload
from utils.plugin_workshop_core.store import WorkshopStore
from utils.plugin_workshop_core.models import Actor, CheckResult, WorkshopError, ValidationReport
from utils.plugin_workshop_core.policy import parse_manifest, normalize_submission

OWNER = Actor("owner", scope_id=1, is_superuser=True)
READER = Actor("owner", scope_id=1)
ADMIN = Actor("webui", is_admin=True)
SUPERUSER = Actor("operator", is_admin=True, is_superuser=True)
SOURCE = "from arclet.entari import command\n@command.on('workshop_ping')\ndef ping():\n    return 'pong'\n"


def manifest(*, value=1):
    return parse_manifest(
        {
            "title": "Counter",
            "description": "A small managed command",
            "commands": ["workshop_ping"],
            "permissions": [],
            "data_description": "No persistent data",
            "configuration": {"value": value},
            "checks": [{"command": "workshop_ping", "expected_contains": "pong"}],
        }
    )


def create(store, *, value=1):
    return store.create_version("counter", {"__init__.py": SOURCE}, manifest(value=value), OWNER)


def accepted(store, record):
    store.mark_validating(record.plugin_name, record.version)
    return store.record_report(
        record.plugin_name,
        record.version,
        ValidationReport(
            "passed",
            record.source_hash,
            (CheckResult("execution", True, "Observed pong"),),
            framework_version="0.19.0+chtholly.1",
            python_version="3.11.15",
            image_id="sha256:" + "a" * 64,
        ),
    )


@pytest.fixture
def store(tmp_path):
    result = WorkshopStore(tmp_path)
    result.initialize()
    return result


@pytest.mark.parametrize(
    "actor",
    [READER, ADMIN, Actor("operator", is_admin=True, is_superuser=1), Actor("operator", is_admin=1, is_superuser=True)],
)
def test_store_mutations_require_strict_superuser_before_storage_access(tmp_path, actor):
    root = tmp_path / "uninitialized"
    untouched = WorkshopStore(root)
    for mutation in (
        lambda: untouched.create_version("counter", {"__init__.py": SOURCE}, manifest(), actor),
        lambda: untouched.begin_change("counter", 1, "a" * 64, actor, action="activate"),
        lambda: untouched.begin_change("counter", 1, "a" * 64, actor, action="rollback"),
        lambda: untouched.begin_change("counter", None, "", actor, action="disable"),
    ):
        with pytest.raises(WorkshopError) as denied:
            mutation()
        assert denied.value.code == "forbidden"
        assert denied.value.status == 403
        assert not root.exists()


def test_admin_is_read_only_while_superuser_without_admin_can_manage_owned_project(store):
    record = accepted(store, create(store))
    assert store.read_files("counter", 1, ADMIN) == {"__init__.py": SOURCE}
    assert store.get_version("counter", 1, READER) == record
    with pytest.raises(WorkshopError) as denied:
        store.begin_change("counter", 1, record.source_hash, ADMIN, action="activate")
    assert denied.value.code == "forbidden"
    operation = store.begin_change("counter", 1, record.source_hash, OWNER, action="activate")
    assert store.complete_change(operation.operation_id, success=True).enabled


def test_activation_requires_owner_scope_acceptance_and_exact_manifest_digest(store):
    first = create(store)
    with pytest.raises(WorkshopError) as pending:
        store.begin_change("counter", 1, first.source_hash, SUPERUSER, action="activate")
    assert pending.value.code == "validation_required"
    accepted(store, first)
    with pytest.raises(WorkshopError) as other_scope:
        store.read_files("counter", 1, Actor("owner", scope_id=2))
    assert other_scope.value.status == 403
    second = accepted(store, create(store, value=2))
    assert first.source_hash != second.source_hash
    with pytest.raises(WorkshopError) as stale:
        store.begin_change("counter", 2, first.source_hash, SUPERUSER, action="activate")
    assert stale.value.code == "stale_hash"
    assert store.pending_operations() == []
    assert store.active_versions() == []
    operation = store.begin_change("counter", 2, second.source_hash, SUPERUSER, action="activate")
    assert store.complete_change(operation.operation_id, success=True).active_version == 2
    assert not store.get_version("counter", 1, READER).ever_active
    assert "approved_by" not in version_payload(second)
    assert "approved_at" not in version_payload(second)


def test_source_tampering_blocks_reads_and_accepted_activation(store, tmp_path):
    record = accepted(store, create(store))
    published = next(tmp_path.rglob("__init__.py"))
    published.write_bytes(SOURCE.replace("pong", "evil").encode())
    for operation in (
        lambda: store.read_files("counter", 1, OWNER),
        lambda: store.begin_change("counter", 1, record.source_hash, SUPERUSER, action="activate"),
    ):
        with pytest.raises(WorkshopError) as changed:
            operation()
        assert changed.value.code == "source_tampered"
    assert store.active_versions() == []


def test_interrupted_update_keeps_committed_version_and_disable_survives_restart(store, tmp_path):
    first, second = accepted(store, create(store)), accepted(store, create(store, value=2))
    initial = store.begin_change("counter", first.version, first.source_hash, SUPERUSER, action="activate")
    assert store.complete_change(initial.operation_id, success=True).active_version == first.version
    pending = store.begin_change("counter", second.version, second.source_hash, SUPERUSER, action="activate")
    restarted = WorkshopStore(tmp_path)
    restarted.initialize()
    assert [record.version for record in restarted.active_versions()] == [first.version]
    assert restarted.pending_operations() == [pending]
    restarted.complete_change(pending.operation_id, success=False, error="Interrupted before commit")
    assert [record.version for record in restarted.active_versions()] == [first.version]
    with pytest.raises(WorkshopError) as never_active:
        restarted.begin_change("counter", second.version, second.source_hash, SUPERUSER, action="rollback")
    assert never_active.value.code == "rollback_unavailable"
    disabled = restarted.begin_change("counter", None, "", SUPERUSER, action="disable")
    assert restarted.complete_change(disabled.operation_id, success=True).enabled is False
    next_restart = WorkshopStore(tmp_path)
    next_restart.initialize()
    assert next_restart.active_versions() == []
    assert next_restart.get_version("counter", first.version, OWNER).ever_active is True
    rollback = next_restart.begin_change("counter", first.version, first.source_hash, SUPERUSER, action="rollback")
    assert next_restart.complete_change(rollback.operation_id, success=True).enabled


def test_revalidation_replaces_inactive_report_but_not_active_or_pending_change(store):
    record = accepted(store, create(store))
    store.mark_validating("counter", 1)
    assert store.get_version("counter", 1, OWNER).report is None
    failed = ValidationReport("failed", record.source_hash, (CheckResult("execution", False, "Missing output"),))
    store.record_report("counter", 1, failed)
    with pytest.raises(WorkshopError) as rejected:
        store.begin_change("counter", 1, record.source_hash, SUPERUSER, action="activate")
    assert rejected.value.code == "validation_required"
    record = accepted(store, record)
    operation = store.begin_change("counter", 1, record.source_hash, SUPERUSER, action="activate")
    with pytest.raises(WorkshopError) as pending:
        store.mark_validating("counter", 1)
    assert pending.value.code == "operation_pending"
    store.complete_change(operation.operation_id, success=True)
    with pytest.raises(WorkshopError) as active:
        store.mark_validating("counter", 1)
    assert active.value.code == "active_version"
    with pytest.raises(WorkshopError) as replaced:
        store.record_report("counter", 1, failed)
    assert replaced.value.code == "validation_conflict"
    assert store.get_version("counter", 1, OWNER).validation_status == "passed"


def test_report_requires_exact_hash_terminal_status_and_real_successful_checks(store):
    record = create(store)
    store.mark_validating("counter", 1)
    with pytest.raises(WorkshopError) as stale:
        store.record_report("counter", 1, ValidationReport("failed", "f" * 64))
    assert stale.value.code == "stale_hash"
    for report in (
        ValidationReport("passed", record.source_hash),
        ValidationReport("passed", record.source_hash, (CheckResult("execution", False),)),
        ValidationReport("validating", record.source_hash),
    ):
        with pytest.raises(WorkshopError):
            store.record_report("counter", 1, report)
    assert store.get_version("counter", 1, OWNER).validation_status == "validating"


def test_legacy_upgrade_preserves_committed_state_audit_and_inert_approval_values(store, tmp_path):
    first = accepted(store, create(store))
    second = accepted(store, create(store, value=2))
    third = accepted(store, create(store, value=3))
    initial = store.begin_change("counter", first.version, first.source_hash, SUPERUSER, action="activate")
    store.complete_change(initial.operation_id, success=True)
    with sqlite3.connect(tmp_path / "workshop.sqlite3") as connection:
        assert not {"approved_by", "approved_at"}.intersection(
            row[1] for row in connection.execute("PRAGMA table_info(versions)")
        )
        connection.execute("ALTER TABLE versions ADD COLUMN approved_by TEXT")
        connection.execute("ALTER TABLE versions ADD COLUMN approved_at TEXT")
        connection.execute(
            "UPDATE versions SET approved_by='legacy-reviewer',approved_at='2025-01-01' WHERE version IN (?,?)",
            (second.version, third.version),
        )
        connection.execute("UPDATE versions SET validation_status='validating' WHERE version=?", (third.version,))
        before_audit = connection.execute("SELECT * FROM operations ORDER BY operation_id").fetchall()
        before_legacy = connection.execute(
            "SELECT version,approved_by,approved_at FROM versions ORDER BY version"
        ).fetchall()
    restarted = WorkshopStore(tmp_path)
    restarted.initialize()
    assert [record.version for record in restarted.active_versions()] == [first.version]
    assert not restarted.get_version("counter", second.version, ADMIN).active
    assert restarted.get_version("counter", third.version, ADMIN).validation_status == "pending"
    with sqlite3.connect(tmp_path / "workshop.sqlite3") as connection:
        assert connection.execute("SELECT * FROM operations ORDER BY operation_id").fetchall() == before_audit
        assert (
            connection.execute("SELECT version,approved_by,approved_at FROM versions ORDER BY version").fetchall()
            == before_legacy
        )
    refreshed = accepted(restarted, third)
    assert refreshed.report.passed
    operation = restarted.begin_change("counter", second.version, second.source_hash, SUPERUSER, action="activate")
    assert restarted.complete_change(operation.operation_id, success=True).active_version == second.version
    assert not hasattr(refreshed, "approved_by")
    with sqlite3.connect(tmp_path / "workshop.sqlite3") as connection:
        assert (
            connection.execute("SELECT version,approved_by,approved_at FROM versions ORDER BY version").fetchall()
            == before_legacy
        )


def test_failed_publication_never_reuses_reserved_version_numbers(tmp_path):
    small = WorkshopStore(tmp_path, max_total_bytes=1)
    small.initialize()
    with pytest.raises(WorkshopError) as quota:
        create(small)
    assert quota.value.code == "limit_exceeded"
    restored = WorkshopStore(tmp_path)
    restored.initialize()
    record = create(restored)
    assert record.version == 2
    assert restored.read_files("counter", record.version, OWNER) == {"__init__.py": SOURCE}


@pytest.mark.parametrize(
    "files",
    [
        {"__init__.py": SOURCE, "../main.py": "pass"},
        {"__init__.py": SOURCE, "CON.txt": "blocked"},
        {"__init__.py": SOURCE, "Part.py": "pass", "part.py": "pass"},
        {"__init__.py": "# coding: utf-7\n" + SOURCE},
    ],
)
def test_source_paths_and_decoding_cannot_disagree_with_immutable_package(files):
    with pytest.raises(WorkshopError):
        normalize_submission("counter", files, manifest())
