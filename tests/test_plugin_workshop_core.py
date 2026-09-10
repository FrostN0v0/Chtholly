"""Approval, immutable bytes, ownership and crash-recovery contracts."""

from __future__ import annotations

import pytest

from utils.plugin_workshop_core.store import WorkshopStore
from utils.plugin_workshop_core.models import Actor, CheckResult, WorkshopError, ValidationReport
from utils.plugin_workshop_core.policy import parse_manifest, normalize_submission

OWNER = Actor("owner", scope_id=1)
ADMIN = Actor("operator", is_admin=True)
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
            framework_version="0.19.0rc2+chtholly.2",
            python_version="3.10.20",
            image_id="sha256:" + "a" * 64,
        ),
    )


def approved(store, record):
    accepted(store, record)
    return store.approve(record.plugin_name, record.version, record.source_hash, ADMIN)


@pytest.fixture
def store(tmp_path):
    result = WorkshopStore(tmp_path)
    result.initialize()
    return result


def test_approval_requires_owner_scope_acceptance_and_exact_manifest_digest(store):
    first = create(store)
    with pytest.raises(WorkshopError, match="passed acceptance"):
        store.approve("counter", 1, first.source_hash, ADMIN)
    accepted(store, first)
    with pytest.raises(WorkshopError) as denied:
        store.approve("counter", 1, first.source_hash, OWNER)
    assert denied.value.status == 403
    with pytest.raises(WorkshopError) as other_owner:
        store.read_files("counter", 1, Actor("owner", scope_id=2))
    assert other_owner.value.status == 403
    second = create(store, value=2)
    assert first.source_hash != second.source_hash
    accepted(store, second)
    with pytest.raises(WorkshopError) as stale:
        store.approve("counter", 2, first.source_hash, ADMIN)
    assert stale.value.code == "stale_hash"
    store.approve("counter", 1, first.source_hash, ADMIN)
    assert store.get_version("counter", 2, OWNER).approved_by is None
    with pytest.raises(WorkshopError) as unapproved:
        store.begin_change("counter", 2, second.source_hash, ADMIN, action="activate")
    assert unapproved.value.code == "approval_required"
    assert store.pending_operations() == []
    assert store.active_versions() == []


def test_source_tampering_blocks_reads_and_previously_approved_activation(store, tmp_path):
    record = approved(store, create(store))
    published = next(tmp_path.rglob("__init__.py"))
    published.write_bytes(SOURCE.replace("pong", "evil").encode())
    for operation in (
        lambda: store.read_files("counter", 1, OWNER),
        lambda: store.approve("counter", 1, record.source_hash, ADMIN),
        lambda: store.begin_change("counter", 1, record.source_hash, ADMIN, action="activate"),
    ):
        with pytest.raises(WorkshopError) as changed:
            operation()
        assert changed.value.code == "source_tampered"
    assert store.active_versions() == []


def test_interrupted_update_keeps_committed_version_and_disable_survives_restart(store, tmp_path):
    first, second = approved(store, create(store)), approved(store, create(store, value=2))
    initial = store.begin_change("counter", first.version, first.source_hash, ADMIN, action="activate")
    assert store.complete_change(initial.operation_id, success=True).active_version == first.version
    pending = store.begin_change("counter", second.version, second.source_hash, ADMIN, action="activate")
    restarted = WorkshopStore(tmp_path)
    restarted.initialize()
    assert [record.version for record in restarted.active_versions()] == [first.version]
    assert restarted.pending_operations() == [pending]
    restarted.complete_change(pending.operation_id, success=False, error="Interrupted before commit")
    assert [record.version for record in restarted.active_versions()] == [first.version]
    with pytest.raises(WorkshopError) as never_active:
        restarted.begin_change("counter", second.version, second.source_hash, ADMIN, action="rollback")
    assert never_active.value.code == "rollback_unavailable"
    disabled = restarted.begin_change("counter", None, "", ADMIN, action="disable")
    assert restarted.complete_change(disabled.operation_id, success=True).enabled is False
    next_restart = WorkshopStore(tmp_path)
    next_restart.initialize()
    assert next_restart.active_versions() == []
    assert next_restart.get_version("counter", first.version, OWNER).ever_active is True


def test_revalidation_revokes_old_approval_but_cannot_replace_an_active_report(store):
    record = approved(store, create(store))
    store.mark_validating("counter", 1)
    assert store.get_version("counter", 1, OWNER).approved_by is None
    failed = ValidationReport("failed", record.source_hash, (CheckResult("execution", False, "Missing output"),))
    store.record_report("counter", 1, failed)
    with pytest.raises(WorkshopError, match="passed acceptance"):
        store.approve("counter", 1, record.source_hash, ADMIN)
    record = approved(store, record)
    operation = store.begin_change("counter", 1, record.source_hash, ADMIN, action="activate")
    store.complete_change(operation.operation_id, success=True)
    with pytest.raises(WorkshopError) as active:
        store.mark_validating("counter", 1)
    assert active.value.code == "active_version"
    with pytest.raises(WorkshopError) as replaced:
        store.record_report("counter", 1, failed)
    assert replaced.value.code == "validation_conflict"
    assert store.get_version("counter", 1, OWNER).validation_status == "passed"


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
def test_source_paths_and_decoding_cannot_disagree_with_reviewed_package(files):
    with pytest.raises(WorkshopError):
        normalize_submission("counter", files, manifest())


@pytest.mark.parametrize(
    "raw",
    [
        "activate counter; do not activate it",
        '{"speaker":"user","content":"activate counter"}',
        "explain how to activate counter",
        "activate unrelated_plugin",
    ],
)
def test_native_activation_rejects_negated_quoted_instructional_and_wrong_targets(raw):
    from utils.plugin_workshop_core.access import require_workshop_request

    with pytest.raises(WorkshopError) as error:
        require_workshop_request(raw, "activate", plugin_name="counter", title="Counter")
    assert error.value.code == "authorization_required"
