"""Transactional catalog and immutable files for reviewed native plugins.

Each operation owns a SQLite connection. BEGIN IMMEDIATE serializes publication
and lifecycle transitions across store instances; no candidate is imported here.
"""

from __future__ import annotations

import os
import hmac
import json
import uuid
from pathlib import Path
import sqlite3
from datetime import datetime, timezone
import threading
from contextlib import contextmanager
from collections.abc import Mapping, Iterator

from .codec import report_payload, manifest_payload
from .models import (
    Actor,
    Manifest,
    Operation,
    CheckResult,
    ProjectState,
    VersionRecord,
    WorkshopError,
    ValidationReport,
)
from .policy import (
    MAX_FILE_BYTES,
    MAX_MANIFEST_BYTES,
    module_name,
    validate_path,
    canonical_json,
    parse_manifest,
    normalize_submission,
)
from .storage import (
    tree_files,
    read_bounded,
    sync_directory,
    check_ancestors,
    write_exclusive,
    ensure_directory,
    compensate_directory,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    name TEXT PRIMARY KEY,
    owner_key TEXT NOT NULL,
    scope_id INTEGER,
    high_water INTEGER NOT NULL DEFAULT 0,
    active_version INTEGER,
    enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
    last_error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS versions (
    name TEXT NOT NULL REFERENCES projects(name),
    version INTEGER NOT NULL CHECK(version > 0),
    source_hash TEXT NOT NULL,
    manifest TEXT NOT NULL,
    files TEXT NOT NULL,
    source_bytes INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    validation_status TEXT NOT NULL DEFAULT 'pending',
    report TEXT,
    approved_by TEXT,
    approved_at TEXT,
    ever_active INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(name, version)
);
CREATE TABLE IF NOT EXISTS operations (
    operation_id TEXT PRIMARY KEY,
    name TEXT NOT NULL REFERENCES projects(name),
    action TEXT NOT NULL,
    target_version INTEGER,
    source_hash TEXT NOT NULL,
    actor_key TEXT NOT NULL,
    previous_version INTEGER,
    previous_enabled INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS one_pending_operation ON operations(name) WHERE status='pending';
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _actor(actor: Actor, *, admin: bool = False) -> None:
    if (
        not isinstance(actor, Actor)
        or not isinstance(actor.key, str)
        or not actor.key.strip()
        or len(actor.key) > 512
        or any(ord(char) < 32 or ord(char) == 127 for char in actor.key)
        or (actor.scope_id is not None and (type(actor.scope_id) is not int or not -(2**63) <= actor.scope_id < 2**63))
        or type(actor.is_admin) is not bool
    ):
        raise WorkshopError("A valid workshop actor is required", code="forbidden", status=403)
    if admin and not actor.is_admin:
        raise WorkshopError("Only an authenticated operator may manage native plugins", code="forbidden", status=403)


def _version_number(version: int) -> None:
    if type(version) is not int or not 1 <= version < 2**63:
        raise WorkshopError("version must be a positive integer")


def _matching_hash(actual: str, supplied: str) -> None:
    if (
        not isinstance(supplied, str)
        or len(supplied) != 64
        or any(char not in "0123456789abcdef" for char in supplied)
        or not hmac.compare_digest(actual, supplied)
    ):
        raise WorkshopError(
            "The supplied digest does not identify this immutable version", code="stale_hash", status=409
        )


def _report_from_json(raw: str) -> ValidationReport:
    value = json.loads(raw)
    return ValidationReport(
        status=value["status"],
        source_hash=value["source_hash"],
        checks=tuple(CheckResult(item["name"], item["passed"], item["detail"]) for item in value["checks"]),
        log=value["log"],
        framework_version=value["framework_version"],
        python_version=value["python_version"],
        image_id=value["image_id"],
    )


def _checked_report(report: ValidationReport) -> str:
    if not isinstance(report, ValidationReport) or report.status not in {
        "passed",
        "failed",
        "unavailable",
        "cancelled",
        "error",
    }:
        raise WorkshopError("Invalid terminal validation report")
    if not isinstance(report.source_hash, str) or len(report.source_hash) != 64:
        raise WorkshopError("Validation report must identify its immutable source digest")
    if not isinstance(report.checks, tuple) or len(report.checks) > 256:
        raise WorkshopError("Validation report contains too many checks", code="limit_exceeded", status=413)
    for check in report.checks:
        if not isinstance(check, CheckResult) or type(check.passed) is not bool:
            raise WorkshopError("Validation check result is malformed")
        if (
            not isinstance(check.name, str)
            or not 1 <= len(check.name) <= 200
            or not isinstance(check.detail, str)
            or len(check.detail) > 8000
        ):
            raise WorkshopError("Validation check text is invalid or too large")
    for value, maximum in (
        (report.log, 65536),
        (report.framework_version, 200),
        (report.python_version, 200),
        (report.image_id, 200),
    ):
        if not isinstance(value, str) or len(value) > maximum:
            raise WorkshopError("Validation report text is invalid or too large")
    if report.status == "passed" and not report.passed:
        raise WorkshopError("A passed report needs successful checks")
    data = canonical_json(report_payload(report))
    if len(data) > 512 * 1024:
        raise WorkshopError("Validation report exceeds 512 KiB", code="limit_exceeded", status=413)
    return data.decode("utf-8")


class WorkshopStore:
    def __init__(self, root: Path, *, max_total_bytes: int = 64 * 1024 * 1024, max_versions: int = 256):
        if (
            type(max_total_bytes) is not int
            or max_total_bytes <= 0
            or type(max_versions) is not int
            or not 1 <= max_versions <= 256
        ):
            raise WorkshopError("Invalid workshop storage limits")
        self.root = Path(root).absolute()
        self.max_total_bytes = max_total_bytes
        self.max_versions = max_versions
        self._sources = self.root / "versions"
        self._staging = self.root / ".staging"
        self._db = self.root / "workshop.sqlite3"
        self._lock = threading.RLock()
        self._initialized = False

    def _check_database_paths(self) -> None:
        for path in (
            self._db,
            Path(str(self._db) + "-wal"),
            Path(str(self._db) + "-shm"),
            Path(str(self._db) + "-journal"),
        ):
            check_ancestors(path)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if not self._initialized:
                raise WorkshopError("Workshop store is not initialized", code="unavailable", status=503)
            connection: sqlite3.Connection | None = None
            try:
                self._check_database_paths()
                connection = sqlite3.connect(self._db, timeout=30, isolation_level=None)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except BaseException as exc:
                if connection is not None and connection.in_transaction:
                    connection.rollback()
                if isinstance(exc, sqlite3.Error):
                    raise WorkshopError("Workshop catalog operation failed", code="storage_error", status=503) from exc
                if isinstance(exc, OSError):
                    raise WorkshopError("Workshop storage operation failed", code="storage_error", status=503) from exc
                raise
            finally:
                if connection is not None:
                    connection.close()

    def initialize(self) -> None:
        with self._lock:
            if self._initialized:
                return
            try:
                for directory in (self.root, self._sources, self._staging):
                    ensure_directory(directory)
                self._check_database_paths()
                connection = sqlite3.connect(self._db, timeout=30)
                try:
                    connection.execute("PRAGMA foreign_keys=ON")
                    connection.execute("PRAGMA synchronous=FULL")
                    connection.executescript(_SCHEMA)
                    connection.execute(
                        "UPDATE projects SET high_water=MAX(high_water, "
                        "COALESCE((SELECT MAX(version) FROM versions WHERE versions.name=projects.name),0))"
                    )
                    connection.execute(
                        "UPDATE versions SET validation_status='pending',report=NULL,approved_by=NULL,approved_at=NULL "
                        "WHERE validation_status='validating'"
                    )
                    connection.commit()
                finally:
                    connection.close()
                self._initialized = True
            except (OSError, sqlite3.Error) as exc:
                raise WorkshopError("Workshop store initialization failed", code="storage_error", status=503) from exc

    @staticmethod
    def _project(connection: sqlite3.Connection, name: str, actor: Actor | None = None) -> sqlite3.Row:
        module_name(name)
        row = connection.execute("SELECT * FROM projects WHERE name=?", (name,)).fetchone()
        if row is None:
            raise WorkshopError("Workshop project does not exist", code="not_found", status=404)
        if (
            actor is not None
            and not actor.is_admin
            and (actor.key != row["owner_key"] or actor.scope_id != row["scope_id"])
        ):
            raise WorkshopError("Workshop project belongs to another owner or scope", code="forbidden", status=403)
        return row

    @staticmethod
    def _row(connection: sqlite3.Connection, name: str, version: int) -> sqlite3.Row:
        _version_number(version)
        row = connection.execute("SELECT * FROM versions WHERE name=? AND version=?", (name, version)).fetchone()
        if row is None:
            raise WorkshopError("Workshop version does not exist", code="not_found", status=404)
        return row

    @staticmethod
    def _record(row: sqlite3.Row, project: sqlite3.Row) -> VersionRecord:
        try:
            manifest = parse_manifest(json.loads(row["manifest"]))
            report = _report_from_json(row["report"]) if row["report"] else None
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkshopError("Workshop catalog metadata is corrupt", code="storage_corrupt", status=409) from exc
        return VersionRecord(
            plugin_name=row["name"],
            version=row["version"],
            source_hash=row["source_hash"],
            manifest=manifest,
            owner_key=project["owner_key"],
            scope_id=project["scope_id"],
            created_at=row["created_at"],
            validation_status=row["validation_status"],
            report=report,
            approved_by=row["approved_by"],
            approved_at=row["approved_at"],
            active=bool(project["enabled"] and project["active_version"] == row["version"]),
            ever_active=bool(row["ever_active"]),
        )

    @staticmethod
    def _state(project: sqlite3.Row) -> ProjectState:
        return ProjectState(project["name"], project["active_version"], bool(project["enabled"]), project["last_error"])

    @staticmethod
    def _operation(row: sqlite3.Row) -> Operation:
        return Operation(
            row["operation_id"],
            row["name"],
            row["action"],
            row["target_version"],
            row["previous_version"],
            bool(row["previous_enabled"]),
        )

    @staticmethod
    def _no_pending(connection: sqlite3.Connection, name: str) -> None:
        if connection.execute("SELECT 1 FROM operations WHERE name=? AND status='pending'", (name,)).fetchone():
            raise WorkshopError(
                "A lifecycle operation is already pending for this project", code="operation_pending", status=409
            )

    def create_version(self, name: str, files: Mapping[str, str], manifest: Manifest, actor: Actor) -> VersionRecord:
        _actor(actor)
        # Detach the shallowly frozen DTO's mutable configuration before hashing.
        if not isinstance(manifest, Manifest):
            raise WorkshopError("manifest must be a Manifest")
        manifest = parse_manifest(manifest_payload(manifest))
        name, checked_files, source_hash = normalize_submission(name, files, manifest)
        manifest_bytes = canonical_json(manifest_payload(manifest))
        encoded = {path: content.encode("utf-8") for path, content in checked_files.items()}
        source_bytes = sum(map(len, encoded.values())) + len(manifest_bytes)
        stage: Path | None = None
        final: Path | None = None
        published = False
        with self._lock:
            # Persist the high-water reservation separately. Failed publication never
            # reuses an identity that a callback, orphan folder or log could retain.
            with self._transaction() as connection:
                module_name(name)
                connection.execute(
                    "INSERT OR IGNORE INTO projects(name,owner_key,scope_id) VALUES (?,?,?)",
                    (name, actor.key, actor.scope_id),
                )
                project = self._project(connection, name, actor)
                count = connection.execute("SELECT COUNT(*) FROM versions WHERE name=?", (name,)).fetchone()[0]
                if count >= self.max_versions:
                    raise WorkshopError("Project version quota exceeded", code="limit_exceeded", status=413)
                version = project["high_water"] + 1
                connection.execute("UPDATE projects SET high_water=? WHERE name=?", (version, name))
            try:
                with self._transaction() as connection:
                    project = self._project(connection, name, actor)
                    count = connection.execute("SELECT COUNT(*) FROM versions WHERE name=?", (name,)).fetchone()[0]
                    if count >= self.max_versions:
                        raise WorkshopError("Project version quota exceeded", code="limit_exceeded", status=413)
                    physical = sum(tree_files(self._sources).values()) + sum(tree_files(self._staging).values())
                    if physical + source_bytes > self.max_total_bytes:
                        raise WorkshopError(
                            "Total immutable source quota exceeded (including publication residue)",
                            code="limit_exceeded",
                            status=413,
                        )
                    stage = self._staging / uuid.uuid4().hex
                    stage.mkdir(mode=0o700)
                    write_exclusive(stage / "manifest.json", manifest_bytes)
                    for path, raw in encoded.items():
                        write_exclusive(stage / "files" / path, raw)
                    for directory in sorted(
                        {(stage / "files" / path).parent for path in encoded},
                        key=lambda item: len(item.parts),
                        reverse=True,
                    ):
                        sync_directory(directory)
                    sync_directory(stage / "files")
                    sync_directory(stage)
                    final = self._sources / module_name(name) / str(version)
                    ensure_directory(final.parent)
                    check_ancestors(final)
                    if final.exists():
                        raise WorkshopError(
                            "Reserved immutable version path is occupied", code="storage_conflict", status=409
                        )
                    os.rename(stage, final)
                    published = True
                    sync_directory(final.parent)
                    sync_directory(self._staging)
                    connection.execute(
                        "INSERT INTO versions(name,version,source_hash,manifest,files,source_bytes,created_at) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (
                            name,
                            version,
                            source_hash,
                            manifest_bytes.decode("utf-8"),
                            canonical_json({path: len(raw) for path, raw in encoded.items()}).decode("utf-8"),
                            source_bytes,
                            _now(),
                        ),
                    )
                    result = self._record(self._row(connection, name, version), project)
                return result
            except BaseException:
                # A successful commit wins even if an interrupt arrives immediately
                # afterwards. Never delete committed immutable source on ambiguity.
                committed: bool | None = None
                try:
                    with self._transaction() as connection:
                        committed = (
                            connection.execute(
                                "SELECT 1 FROM versions WHERE name=? AND version=?", (name, version)
                            ).fetchone()
                            is not None
                        )
                except BaseException:
                    pass
                if committed is False and published and final is not None:
                    compensate_directory(final)
                if stage is not None:
                    compensate_directory(stage)
                raise

    def get_version(self, name: str, version: int, actor: Actor) -> VersionRecord:
        _actor(actor)
        with self._transaction() as connection:
            project = self._project(connection, name, actor)
            return self._record(self._row(connection, name, version), project)

    def _read_files(self, row: sqlite3.Row, project: sqlite3.Row) -> dict[str, str]:
        record = self._record(row, project)
        root = self._sources / module_name(record.plugin_name) / str(record.version)
        try:
            manifest_bytes = read_bounded(root / "manifest.json", MAX_MANIFEST_BYTES)
            if not hmac.compare_digest(manifest_bytes, row["manifest"].encode("utf-8")):
                raise WorkshopError("Immutable manifest has changed", code="source_tampered", status=409)
            registered = json.loads(row["files"])
            if not isinstance(registered, dict) or not 1 <= len(registered) <= 32:
                raise WorkshopError("Immutable file inventory is corrupt", code="storage_corrupt", status=409)
            expected = {"manifest.json": len(manifest_bytes)}
            result: dict[str, str] = {}
            for path, size in registered.items():
                validate_path(path)
                if type(size) is not int or not 0 <= size <= MAX_FILE_BYTES:
                    raise WorkshopError("Immutable file size metadata is corrupt", code="storage_corrupt", status=409)
                raw = read_bounded(root / "files" / path, size)
                if len(raw) != size:
                    raise WorkshopError("Immutable source file size changed", code="source_tampered", status=409)
                result[path] = raw.decode("utf-8")
                expected[f"files/{path}"] = size
            if tree_files(root, max_entries=300) != expected:
                raise WorkshopError("Immutable source inventory changed", code="source_tampered", status=409)
            _, _, digest = normalize_submission(record.plugin_name, result, record.manifest)
            if not hmac.compare_digest(digest, record.source_hash):
                raise WorkshopError(
                    "Immutable source digest does not match its catalog", code="source_tampered", status=409
                )
            return result
        except FileNotFoundError as exc:
            raise WorkshopError("Immutable source is missing", code="source_missing", status=409) from exc
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise WorkshopError(
                "Immutable source encoding or metadata changed", code="source_tampered", status=409
            ) from exc

    def read_files(self, name: str, version: int, actor: Actor) -> dict[str, str]:
        _actor(actor)
        with self._transaction() as connection:
            project = self._project(connection, name, actor)
            return self._read_files(self._row(connection, name, version), project)

    def list_projects(self, actor: Actor, *, limit: int = 100, offset: int = 0) -> list[dict[str, object]]:
        _actor(actor)
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset < 2**63:
            raise WorkshopError("Project pagination requires limit 1..100 and a nonnegative offset")
        with self._transaction() as connection:
            query = (
                "SELECT p.*, (SELECT MAX(v.version) FROM versions v WHERE v.name=p.name) AS latest_version "
                "FROM projects p"
            )
            params: list[object] = []
            if not actor.is_admin:
                query += " WHERE owner_key=? AND scope_id IS ?"
                params.extend((actor.key, actor.scope_id))
            query += " ORDER BY p.name LIMIT ? OFFSET ?"
            params.extend((limit, offset))
            result = []
            for project in connection.execute(query, params):
                title = ""
                if project["latest_version"] is not None:
                    title = self._record(
                        self._row(connection, project["name"], project["latest_version"]), project
                    ).manifest.title
                result.append(
                    {
                        "plugin_name": project["name"],
                        "owner_key": project["owner_key"],
                        "scope_id": project["scope_id"],
                        "active_version": project["active_version"],
                        "enabled": bool(project["enabled"]),
                        "last_error": project["last_error"],
                        "latest_version": project["latest_version"],
                        "title": title,
                    }
                )
            return result

    def list_versions(self, name: str, actor: Actor) -> list[VersionRecord]:
        _actor(actor)
        with self._transaction() as connection:
            project = self._project(connection, name, actor)
            return [
                self._record(row, project)
                for row in connection.execute("SELECT * FROM versions WHERE name=? ORDER BY version DESC", (name,))
            ]

    def mark_validating(self, name: str, version: int) -> None:
        with self._transaction() as connection:
            project = self._project(connection, name)
            row = self._row(connection, name, version)
            self._no_pending(connection, name)
            if project["enabled"] and project["active_version"] == version:
                raise WorkshopError(
                    "An enabled active version cannot be revalidated in place", code="active_version", status=409
                )
            if row["validation_status"] == "validating":
                raise WorkshopError("This version is already being validated", code="validation_pending", status=409)
            self._read_files(row, project)
            connection.execute(
                "UPDATE versions SET validation_status='validating',report=NULL,approved_by=NULL,approved_at=NULL "
                "WHERE name=? AND version=?",
                (name, version),
            )

    def record_report(self, name: str, version: int, report: ValidationReport) -> VersionRecord:
        encoded = _checked_report(report)
        with self._transaction() as connection:
            project = self._project(connection, name)
            row = self._row(connection, name, version)
            self._no_pending(connection, name)
            _matching_hash(row["source_hash"], report.source_hash)
            if (
                row["validation_status"] != "validating"
                or row["approved_by"] is not None
                or (project["enabled"] and project["active_version"] == version)
            ):
                raise WorkshopError(
                    "Validation report cannot replace an approved, active or nonvalidating record",
                    code="validation_conflict",
                    status=409,
                )
            self._read_files(row, project)
            connection.execute(
                "UPDATE versions SET validation_status=?,report=? WHERE name=? AND version=?",
                (report.status, encoded, name, version),
            )
            return self._record(self._row(connection, name, version), project)

    @staticmethod
    def _passed(record: VersionRecord) -> None:
        if (
            record.validation_status != "passed"
            or record.report is None
            or not record.report.passed
            or record.report.source_hash != record.source_hash
        ):
            raise WorkshopError(
                "This exact version needs a passed acceptance report", code="validation_required", status=409
            )

    def approve(self, name: str, version: int, source_hash: str, actor: Actor) -> VersionRecord:
        _actor(actor, admin=True)
        with self._transaction() as connection:
            project = self._project(connection, name, actor)
            row = self._row(connection, name, version)
            self._no_pending(connection, name)
            _matching_hash(row["source_hash"], source_hash)
            record = self._record(row, project)
            self._passed(record)
            self._read_files(row, project)
            if row["approved_by"] is None:
                connection.execute(
                    "UPDATE versions SET approved_by=?,approved_at=? WHERE name=? AND version=?",
                    (actor.key, _now(), name, version),
                )
            return self._record(self._row(connection, name, version), project)

    def begin_change(self, name: str, version: int | None, source_hash: str, actor: Actor, *, action: str) -> Operation:
        _actor(actor, admin=True)
        if not isinstance(action, str) or action not in {"activate", "rollback", "disable"}:
            raise WorkshopError("Unknown workshop lifecycle action")
        if not isinstance(source_hash, str) or len(source_hash) > 64:
            raise WorkshopError("Invalid lifecycle source digest")
        if version is not None:
            _version_number(version)
        rejected: WorkshopError | None = None
        with self._transaction() as connection:
            project = self._project(connection, name, actor)
            operation = Operation(
                uuid.uuid4().hex, name, action, version, project["active_version"], bool(project["enabled"])
            )
            # Persist every well-formed operator attempt on an existing project,
            # including rejected approval, integrity and concurrent-operation checks.
            connection.execute(
                "INSERT INTO operations(operation_id,name,action,target_version,source_hash,actor_key,"
                "previous_version,previous_enabled,created_at,status) VALUES (?,?,?,?,?,?,?,?,?,'checking')",
                (
                    operation.operation_id,
                    name,
                    action,
                    version,
                    source_hash,
                    actor.key,
                    operation.previous_version,
                    int(operation.previous_enabled),
                    _now(),
                ),
            )
            try:
                self._no_pending(connection, name)
                if action == "disable":
                    if version is not None or source_hash:
                        raise WorkshopError("Disable must not specify a target version or digest")
                else:
                    if version is None:
                        raise WorkshopError("Activation and rollback require an exact target version")
                    row = self._row(connection, name, version)
                    _matching_hash(row["source_hash"], source_hash)
                    record = self._record(row, project)
                    self._passed(record)
                    if not record.approved_by or not record.approved_at:
                        raise WorkshopError(
                            "This exact version requires human native-code approval",
                            code="approval_required",
                            status=409,
                        )
                    if action == "rollback" and not record.ever_active:
                        raise WorkshopError(
                            "Rollback target has never been active", code="rollback_unavailable", status=409
                        )
                    self._read_files(row, project)
            except WorkshopError as exc:
                rejected = exc
                connection.execute(
                    "UPDATE operations SET status='failed',error=?,completed_at=? WHERE operation_id=?",
                    (str(exc)[:8000], _now(), operation.operation_id),
                )
            else:
                connection.execute(
                    "UPDATE operations SET status='pending' WHERE operation_id=?", (operation.operation_id,)
                )
        if rejected is not None:
            raise rejected
        return operation

    def complete_change(self, operation_id: str, *, success: bool, error: str = "") -> ProjectState:
        if (
            not isinstance(operation_id, str)
            or len(operation_id) != 32
            or type(success) is not bool
            or not isinstance(error, str)
        ):
            raise WorkshopError("Invalid lifecycle completion")
        with self._transaction() as connection:
            operation = connection.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
            if operation is None:
                raise WorkshopError("Lifecycle operation does not exist", code="not_found", status=404)
            project = self._project(connection, operation["name"])
            if operation["status"] != "pending":
                if (operation["status"] == "succeeded") != success:
                    raise WorkshopError(
                        "Lifecycle operation already settled with a different outcome",
                        code="operation_conflict",
                        status=409,
                    )
                return self._state(project)
            if project["active_version"] != operation["previous_version"] or bool(project["enabled"]) != bool(
                operation["previous_enabled"]
            ):
                raise WorkshopError(
                    "Committed lifecycle state changed while operation was pending",
                    code="operation_conflict",
                    status=409,
                )
            if success:
                if operation["action"] == "disable":
                    connection.execute("UPDATE projects SET enabled=0,last_error='' WHERE name=?", (operation["name"],))
                else:
                    row = self._row(connection, operation["name"], operation["target_version"])
                    record = self._record(row, project)
                    self._passed(record)
                    _matching_hash(row["source_hash"], operation["source_hash"])
                    if not record.approved_by or not record.approved_at:
                        raise WorkshopError(
                            "Approval disappeared during lifecycle operation", code="approval_required", status=409
                        )
                    self._read_files(row, project)
                    connection.execute(
                        "UPDATE projects SET active_version=?,enabled=1,last_error='' WHERE name=?",
                        (operation["target_version"], operation["name"]),
                    )
                    connection.execute(
                        "UPDATE versions SET ever_active=1 WHERE name=? AND version=?",
                        (operation["name"], operation["target_version"]),
                    )
            else:
                connection.execute(
                    "UPDATE projects SET last_error=? WHERE name=?",
                    (error[:8000] or "Lifecycle operation failed", operation["name"]),
                )
            connection.execute(
                "UPDATE operations SET status=?,error=?,completed_at=? WHERE operation_id=?",
                ("succeeded" if success else "failed", "" if success else error[:8000], _now(), operation_id),
            )
            return self._state(self._project(connection, operation["name"]))

    def record_runtime_error(self, name: str, error: str) -> None:
        """Persist restoration diagnostics without changing committed intent."""
        if not isinstance(error, str):
            raise WorkshopError("Runtime error must be text")
        with self._transaction() as connection:
            self._project(connection, name)
            connection.execute("UPDATE projects SET last_error=? WHERE name=?", (error[:8000], name))

    def active_versions(self) -> list[VersionRecord]:
        with self._transaction() as connection:
            return [
                self._record(self._row(connection, project["name"], project["active_version"]), project)
                for project in connection.execute("SELECT * FROM projects WHERE enabled=1 ORDER BY name")
            ]

    def pending_operations(self) -> list[Operation]:
        with self._transaction() as connection:
            return [
                self._operation(row)
                for row in connection.execute(
                    "SELECT * FROM operations WHERE status='pending' ORDER BY created_at,operation_id"
                )
            ]
