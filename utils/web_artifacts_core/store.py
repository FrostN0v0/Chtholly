"""Stdlib-only immutable storage for versioned web artifacts.

The store deliberately exposes synchronous methods.  Runtime and HTTP callers
can move those calls to a worker thread without importing Entari or any other
framework into this package.
"""

from __future__ import annotations

from io import BytesIO
import os
import re
import hmac
import math
import stat
import time
import uuid
import shutil
import hashlib
from pathlib import Path
import secrets
import sqlite3
import zipfile
import threading
from collections.abc import Mapping, Callable, Sequence

from .errors import ArtifactError, ArtifactNotFound, ArtifactLimitError, ArtifactAccessDenied
from .models import Artifact, ArtifactOwner, ArtifactLimits, ArtifactFileInfo
from .validation import (
    ValidatedFile,
    is_png,
    path_casefold,
    source_digest,
    validate_entry,
    validate_title,
    sniff_image_mime,
    validate_turn_key,
    validate_ttl_hours,
    validate_input_files,
    validate_delete_paths,
    mime_for_artifact_path,
    normalize_artifact_path,
)

_DB_NAME = "artifacts.sqlite3"
_ARTIFACTS_DIR = "artifacts"
_STAGING_DIR = ".staging"
_ARTIFACT_REF_RE = re.compile(r"^wa_[A-Za-z0-9_-]{20,128}$")
_PROJECT_REF_RE = re.compile(r"^wp_[A-Za-z0-9_-]{20,128}$")
_TOKEN_RE = re.compile(r"^wt_[A-Za-z0-9_-]{20,128}$")
_STAGE_RE = re.compile(r"^stage-(\d+)-([0-9a-f]{32})$")
_PURGE_RE = re.compile(r"^purge-(\d+)-([0-9a-f]{32})$")
_MAX_LIST_LIMIT = 100
_PURGE_BATCH_SIZE = 100
_ORPHAN_RECOVERY_BATCH_SIZE = 100
_RECOVERY_INSPECTION_LIMIT = 4096
_STAGE_MAX_AGE_SECONDS = 60 * 60

_SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_ref TEXT NOT NULL UNIQUE,
    project_ref TEXT NOT NULL,
    version INTEGER NOT NULL,
    owner_scope INTEGER NOT NULL,
    owner_user TEXT NOT NULL,
    title TEXT NOT NULL,
    entry TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    token TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    source_bytes INTEGER NOT NULL,
    zip_bytes INTEGER NOT NULL,
    source_sha256 TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0 CHECK (revoked IN (0, 1)),
    preview_bytes INTEGER NOT NULL DEFAULT 0,
    preview_sha256 TEXT NOT NULL DEFAULT '',
    UNIQUE(project_ref, version)
);
CREATE TABLE IF NOT EXISTS artifact_files (
    artifact_ref TEXT NOT NULL REFERENCES artifacts(artifact_ref) ON DELETE CASCADE,
    path TEXT NOT NULL,
    mime TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    encoding TEXT NOT NULL,
    PRIMARY KEY (artifact_ref, path)
);
CREATE TABLE IF NOT EXISTS turn_publications (
    owner_scope INTEGER NOT NULL,
    owner_user TEXT NOT NULL,
    turn_key TEXT NOT NULL,
    publication_count INTEGER NOT NULL,
    PRIMARY KEY (owner_scope, owner_user, turn_key)
);
CREATE TABLE IF NOT EXISTS project_versions (
    project_ref TEXT PRIMARY KEY,
    high_water INTEGER NOT NULL CHECK (high_water > 0)
);
CREATE INDEX IF NOT EXISTS artifacts_owner_active_idx
    ON artifacts(owner_scope, owner_user, revoked, expires_at, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS artifacts_project_idx
    ON artifacts(project_ref, version);
"""
_REQUIRED_ARTIFACT_COLUMNS = frozenset(
    {
        "artifact_ref",
        "project_ref",
        "version",
        "owner_scope",
        "owner_user",
        "title",
        "entry",
        "created_at",
        "expires_at",
        "token",
        "token_hash",
        "source_bytes",
        "zip_bytes",
        "source_sha256",
        "revoked",
        "preview_bytes",
        "preview_sha256",
    }
)
_REQUIRED_FILE_COLUMNS = frozenset({"artifact_ref", "path", "mime", "size", "sha256", "encoding"})
_REQUIRED_TURN_COLUMNS = frozenset({"owner_scope", "owner_user", "turn_key", "publication_count"})
_REQUIRED_PROJECT_VERSION_COLUMNS = frozenset({"project_ref", "high_water"})


class ArtifactStore:
    """Persistent, quota-enforced store for immutable web artifact versions."""

    def __init__(
        self,
        root: Path,
        *,
        limits: ArtifactLimits | None = None,
        clock: Callable[[], float] = time.time,
        read_only: bool = False,
    ) -> None:
        if not isinstance(root, Path):
            root = Path(root)
        if not callable(clock):
            raise ValueError("clock must be callable")
        self.root = root.expanduser().absolute()
        self.limits = limits or ArtifactLimits()
        self.clock = clock
        self.read_only = bool(read_only)
        self._artifact_root = self.root / _ARTIFACTS_DIR
        self._staging_root = self.root / _STAGING_DIR
        self._db_path = self.root / _DB_NAME
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    @property
    def db_path(self) -> Path:
        """Path to the private SQLite catalog."""

        return self._db_path

    def initialize(self) -> ArtifactStore:
        """Create a writable store or validate an existing read-only store."""

        with self._lock:
            if self._conn is not None:
                return self
            if self.read_only:
                if (
                    self.root.is_symlink()
                    or self._artifact_root.is_symlink()
                    or self._db_path.is_symlink()
                    or not self.root.is_dir()
                    or not self._db_path.is_file()
                    or not self._artifact_root.is_dir()
                ):
                    raise ArtifactError("read-only artifact store is not initialized")
                database_uri = self._db_path.as_uri() + "?mode=ro"
                try:
                    conn = sqlite3.connect(
                        database_uri,
                        uri=True,
                        isolation_level=None,
                        check_same_thread=False,
                        timeout=30.0,
                    )
                except sqlite3.Error as exc:
                    raise ArtifactError("unable to open read-only artifact store") from exc
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA query_only=ON")
                conn.execute("PRAGMA foreign_keys=ON")
                self._conn = conn
                try:
                    self._validate_schema(conn, require_project_versions=False)
                except BaseException:
                    conn.close()
                    self._conn = None
                    raise
                return self

            conn: sqlite3.Connection | None = None
            try:
                self._ensure_store_directory(self.root, create=True)
                self._ensure_store_directory(self._artifact_root, create=True)
                self._ensure_store_directory(self._staging_root, create=True)
                if self._db_path.is_symlink() or (self._db_path.exists() and not self._db_path.is_file()):
                    raise ArtifactError("artifact database path is not a regular file")
                conn = sqlite3.connect(
                    str(self._db_path),
                    isolation_level=None,
                    check_same_thread=False,
                    timeout=30.0,
                )
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=DELETE")
                conn.execute("PRAGMA synchronous=FULL")
                conn.execute("PRAGMA foreign_keys=ON")
                conn.executescript(_SCHEMA)
                self._validate_schema(conn)
                conn.execute("BEGIN IMMEDIATE")
                try:
                    self._backfill_project_versions(conn)
                    self._recover_orphan_artifacts(conn)
                    conn.commit()
                except BaseException:
                    self._rollback_transaction(conn)
                    raise
            except (OSError, sqlite3.Error, ArtifactError) as exc:
                if conn is not None:
                    conn.close()
                if isinstance(exc, ArtifactError):
                    raise
                raise ArtifactError("unable to initialize artifact store") from exc
            self._conn = conn
            return self

    def close(self) -> None:
        """Close the SQLite handle; a later ``initialize`` may reopen it."""

        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def publish(
        self,
        owner: ArtifactOwner,
        title: str,
        files: Sequence[Mapping[str, str]],
        *,
        entry: str = "index.html",
        previous_ref: str = "",
        ttl_hours: float = 24,
        turn_key: str = "",
        delete_paths: Sequence[str] = (),
    ) -> Artifact:
        """Validate, stage, and atomically publish one immutable version."""

        self._require_writable()
        self._require_owner(owner)
        clean_title = validate_title(title)
        clean_entry = validate_entry(entry)
        clean_ttl = validate_ttl_hours(ttl_hours, self.limits.max_ttl_hours)
        clean_turn_key = validate_turn_key(turn_key)
        if not isinstance(previous_ref, str):
            raise ArtifactError("previous_ref must be a string")
        submitted = validate_input_files(files, self.limits)
        clean_deletes = validate_delete_paths(delete_paths)
        if clean_deletes and not previous_ref:
            raise ArtifactError("delete_paths require previous_ref")

        with self._lock:
            conn = self._connection()
            now = self._now()
            previous_row: sqlite3.Row | None = None
            previous_files: tuple[ValidatedFile, ...] = ()
            if previous_ref:
                previous_row = self._find_row_by_ref(conn, previous_ref)
                self._authorize_exact_owner(previous_row, owner)
                self._require_active(previous_row, now)
                previous_files = self._load_validated_files(conn, previous_row)

            merged = self._merge_files(previous_files, submitted, clean_deletes)
            self._validate_project(merged, clean_entry)
            source_bytes = sum(len(item.data) for item in merged)
            zip_data = self._build_zip(merged)
            if len(zip_data) > self.limits.max_zip_bytes:
                raise ArtifactLimitError("project exceeds max_zip_bytes")
            digest = source_digest(merged)
            artifact_ref = self._new_ref("wa_", _ARTIFACT_REF_RE)
            project_ref = (
                previous_row["project_ref"] if previous_row is not None else self._new_ref("wp_", _PROJECT_REF_RE)
            )
            token = self._new_ref("wt_", _TOKEN_RE)
            expires_at = now + clean_ttl * 3600.0
            if not math.isfinite(expires_at):
                raise ArtifactError("artifact expiration is out of range")
            stage = self._staging_root / f"stage-{int(now)}-{uuid.uuid4().hex}"
            final_dir = self._artifact_root / artifact_ref
            moved_to_final = False
            transaction_started = False

            try:
                self._require_generated_directory(self._artifact_root, self.root)
                self._require_generated_directory(self._staging_root, self.root)
                # Recovery takes the same SQLite writer lock before scanning final directories.
                # Do not expose the final directory until that lock is held.
                self._write_staging(stage, merged, zip_data)
                conn.execute("BEGIN IMMEDIATE")
                transaction_started = True
                if previous_row is not None:
                    current_previous = self._find_row_by_ref(conn, previous_ref)
                    self._authorize_exact_owner(current_previous, owner)
                    self._require_active(current_previous, now)
                    if (
                        current_previous["project_ref"] != previous_row["project_ref"]
                        or current_previous["version"] != previous_row["version"]
                    ):
                        raise ArtifactError("previous artifact changed during publication")
                if final_dir.exists() or final_dir.is_symlink():
                    raise ArtifactError("artifact reference path is already occupied")
                version = self._allocate_project_version(conn, project_ref, is_new=previous_row is None)
                self._enforce_publish_quotas(
                    conn,
                    owner,
                    now,
                    source_bytes=source_bytes,
                    zip_bytes=len(zip_data),
                    turn_key=clean_turn_key,
                )
                os.replace(stage, final_dir)
                self._fsync_directory(self._artifact_root)
                moved_to_final = True
                conn.execute(
                    """
                    INSERT INTO artifacts (
                        artifact_ref, project_ref, version, owner_scope, owner_user,
                        title, entry, created_at, expires_at, token, token_hash,
                        source_bytes, zip_bytes, source_sha256, revoked,
                        preview_bytes, preview_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, '')
                    """,
                    (
                        artifact_ref,
                        project_ref,
                        version,
                        owner.scope_id,
                        owner.user_id,
                        clean_title,
                        clean_entry,
                        now,
                        expires_at,
                        token,
                        self._token_hash(token),
                        source_bytes,
                        len(zip_data),
                        digest,
                    ),
                )
                conn.executemany(
                    """INSERT INTO artifact_files (artifact_ref, path, mime, size, sha256, encoding)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    [
                        (artifact_ref, item.path, item.mime, len(item.data), item.sha256, item.encoding)
                        for item in merged
                    ],
                )
                if clean_turn_key:
                    conn.execute(
                        """
                        INSERT INTO turn_publications (owner_scope, owner_user, turn_key, publication_count)
                        VALUES (?, ?, ?, 1)
                        ON CONFLICT(owner_scope, owner_user, turn_key)
                        DO UPDATE SET publication_count = publication_count + 1
                        """,
                        (owner.scope_id, owner.user_id, clean_turn_key),
                    )
                conn.commit()
                transaction_started = False
                moved_to_final = False
                return Artifact(
                    artifact_ref=artifact_ref,
                    project_ref=project_ref,
                    version=version,
                    title=clean_title,
                    entry=clean_entry,
                    created_at=now,
                    expires_at=expires_at,
                    token=token,
                    files=tuple(item.info for item in merged),
                    source_bytes=source_bytes,
                    zip_bytes=len(zip_data),
                    source_sha256=digest,
                )
            except BaseException:
                if transaction_started:
                    self._rollback_transaction(conn)
                committed = False
                try:
                    committed = (
                        conn.execute("SELECT 1 FROM artifacts WHERE artifact_ref = ?", (artifact_ref,)).fetchone()
                        is not None
                    )
                except sqlite3.Error:
                    pass
                if not committed and moved_to_final:
                    self._safe_remove_generated_dir(final_dir, self._artifact_root)
                self._safe_remove_generated_dir(stage, self._staging_root)
                raise

    def get_owned(self, ref: str, owner: ArtifactOwner, *, admin: bool = False) -> Artifact:
        """Return active metadata for an owner, or an admin in the same scope."""

        self._require_owner(owner)
        with self._lock:
            conn = self._connection()
            row = self._find_row_by_ref(conn, ref)
            self._authorize_row(row, owner, admin=admin)
            self._require_active(row, self._now())
            return self._artifact_from_row(conn, row)

    def list_owned(self, owner: ArtifactOwner, *, admin: bool = False, limit: int = 10) -> list[Artifact]:
        """List active metadata newest first inside the owner's scope."""

        self._require_owner(owner)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > _MAX_LIST_LIMIT:
            raise ArtifactError(f"limit must be an integer from 1 to {_MAX_LIST_LIMIT}")
        with self._lock:
            conn = self._connection()
            now = self._now()
            if admin is True:
                rows = conn.execute(
                    """
                    SELECT * FROM artifacts
                    WHERE owner_scope = ? AND revoked = 0 AND expires_at > ?
                    ORDER BY created_at DESC, id DESC LIMIT ?
                    """,
                    (owner.scope_id, now, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM artifacts
                    WHERE owner_scope = ? AND owner_user = ? AND revoked = 0 AND expires_at > ?
                    ORDER BY created_at DESC, id DESC LIMIT ?
                    """,
                    (owner.scope_id, owner.user_id, now, limit),
                ).fetchall()
            return [self._artifact_from_row(conn, row) for row in rows]

    def read_owned_file(
        self,
        ref: str,
        owner: ArtifactOwner,
        path: str,
        *,
        admin: bool = False,
    ) -> tuple[bytes, str]:
        """Read one registered source file after ownership and integrity checks."""

        self._require_owner(owner)
        with self._lock:
            conn = self._connection()
            row = self._find_row_by_ref(conn, ref)
            self._authorize_row(row, owner, admin=admin)
            self._require_active(row, self._now())
            normalized = normalize_artifact_path(path)
            file_row = self._find_file_row(conn, row["artifact_ref"], normalized)
            data = self._read_source_file(row, file_row)
            return data, str(file_row["mime"])

    def zip_owned(self, ref: str, owner: ArtifactOwner, *, admin: bool = False) -> bytes:
        """Return the exact deterministic source archive for an owned artifact."""

        self._require_owner(owner)
        with self._lock:
            conn = self._connection()
            row = self._find_row_by_ref(conn, ref)
            self._authorize_row(row, owner, admin=admin)
            self._require_active(row, self._now())
            return self._read_zip(conn, row)

    def get_public(self, token: str) -> Artifact:
        """Resolve a public capability token to active metadata."""

        with self._lock:
            conn = self._connection()
            row = self._find_public_row(conn, token)
            self._require_active(row, self._now())
            return self._artifact_from_row(conn, row)

    def read_public_file(self, token: str, path: str) -> tuple[bytes, str]:
        """Read one registered source file through a public capability token."""

        with self._lock:
            conn = self._connection()
            row = self._find_public_row(conn, token)
            self._require_active(row, self._now())
            normalized = normalize_artifact_path(path)
            file_row = self._find_file_row(conn, row["artifact_ref"], normalized)
            data = self._read_source_file(row, file_row)
            return data, str(file_row["mime"])

    def zip_public(self, token: str) -> bytes:
        """Return the source archive through a public capability token."""

        with self._lock:
            conn = self._connection()
            row = self._find_public_row(conn, token)
            self._require_active(row, self._now())
            return self._read_zip(conn, row)

    def preview_public(self, token: str) -> bytes:
        """Return an attached immutable PNG derivative through a public token."""

        with self._lock:
            conn = self._connection()
            row = self._find_public_row(conn, token)
            self._require_active(row, self._now())
            return self._read_preview(row)

    def attach_preview(self, ref: str, owner: ArtifactOwner, data: bytes) -> None:
        """Atomically attach one immutable PNG derivative to an artifact."""

        self._require_writable()
        self._require_owner(owner)
        if not isinstance(data, bytes):
            raise ArtifactError("preview data must be bytes")
        if not data or not is_png(data):
            raise ArtifactError("preview must be a PNG image")
        if len(data) > self.limits.max_preview_bytes:
            raise ArtifactLimitError("preview exceeds max_preview_bytes")

        with self._lock:
            conn = self._connection()
            row = self._find_row_by_ref(conn, ref)
            self._authorize_exact_owner(row, owner)
            self._require_active(row, self._now())
            if int(row["preview_bytes"]) > 0 or str(row["preview_sha256"]):
                raise ArtifactError("artifact already has an immutable preview")
            artifact_dir = self._artifact_dir(str(row["artifact_ref"]))
            self._require_generated_directory(artifact_dir, self._artifact_root)
            preview_path = artifact_dir / "preview.png"
            if preview_path.exists() or preview_path.is_symlink():
                raise ArtifactError("artifact preview path is already occupied")
            temporary = artifact_dir / f"preview.tmp-{uuid.uuid4().hex}"
            installed = False
            try:
                conn.execute("BEGIN IMMEDIATE")
                current = self._find_row_by_ref(conn, ref)
                self._authorize_exact_owner(current, owner)
                self._require_active(current, self._now())
                if int(current["preview_bytes"]) > 0 or str(current["preview_sha256"]):
                    raise ArtifactError("artifact already has an immutable preview")
                self._require_generated_directory(artifact_dir, self._artifact_root)
                if preview_path.exists() or preview_path.is_symlink():
                    raise ArtifactError("artifact preview path is already occupied")
                if self._total_disk_bytes(conn) + len(data) > self.limits.max_total_bytes:
                    raise ArtifactLimitError("store exceeds max_total_bytes")
                with temporary.open("xb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, preview_path)
                installed = True
                digest = hashlib.sha256(data).hexdigest()
                conn.execute(
                    "UPDATE artifacts SET preview_bytes = ?, preview_sha256 = ? WHERE artifact_ref = ?",
                    (len(data), digest, ref),
                )
                conn.commit()
            except BaseException:
                self._rollback_transaction(conn)
                committed = False
                try:
                    current = self._find_row_by_ref(conn, ref)
                    committed = int(current["preview_bytes"]) > 0 and bool(str(current["preview_sha256"]))
                except (ArtifactError, sqlite3.Error):
                    pass
                if not committed and installed:
                    self._safe_remove_file(preview_path, artifact_dir)
                self._safe_remove_file(temporary, artifact_dir)
                raise

    def revoke(self, ref: str, owner: ArtifactOwner, *, admin: bool = False) -> bool:
        """Revoke all public routes for an artifact, idempotently."""

        self._require_writable()
        self._require_owner(owner)
        with self._lock:
            conn = self._connection()
            row = self._find_row_by_ref(conn, ref)
            self._authorize_row(row, owner, admin=admin)
            if int(row["revoked"]):
                return False
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("UPDATE artifacts SET revoked = 1 WHERE artifact_ref = ?", (ref,))
                conn.commit()
            except BaseException:
                self._rollback_transaction(conn)
                raise
            return True

    def purge_expired(self) -> int:
        """Remove expired/revoked rows and bounded safe staging debris."""

        if self.read_only:
            return 0
        with self._lock:
            conn = self._connection()
            now = self._now()
            moved: list[tuple[Path, Path]] = []
            removed_count = 0
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(
                    """
                    SELECT artifact_ref, project_ref FROM artifacts
                    WHERE revoked = 1 OR expires_at <= ?
                    ORDER BY expires_at ASC, id ASC LIMIT ?
                    """,
                    (now, _PURGE_BATCH_SIZE),
                ).fetchall()
                projects_to_check: set[str] = set()
                for row in rows:
                    ref = str(row["artifact_ref"])
                    projects_to_check.add(str(row["project_ref"]))
                    final_dir = self._artifact_dir(ref)
                    if final_dir.exists() or final_dir.is_symlink():
                        self._require_generated_directory(final_dir, self._artifact_root)
                        trash = self._staging_root / f"purge-{int(now)}-{uuid.uuid4().hex}"
                        os.replace(final_dir, trash)
                        moved.append((final_dir, trash))
                    conn.execute("DELETE FROM artifacts WHERE artifact_ref = ?", (ref,))
                    removed_count += 1
                for project_ref in projects_to_check:
                    conn.execute(
                        """
                        DELETE FROM project_versions
                        WHERE project_ref = ?
                          AND NOT EXISTS (
                              SELECT 1 FROM artifacts WHERE project_ref = ?
                          )
                        """,
                        (project_ref, project_ref),
                    )
                conn.commit()
            except BaseException:
                self._rollback_transaction(conn)
                for final_dir, trash in reversed(moved):
                    committed_deleted = False
                    try:
                        committed_deleted = (
                            conn.execute("SELECT 1 FROM artifacts WHERE artifact_ref = ?", (final_dir.name,)).fetchone()
                            is None
                        )
                    except sqlite3.Error:
                        committed_deleted = False
                    if committed_deleted:
                        self._safe_remove_generated_dir(trash, self._staging_root)
                    elif trash.exists() and not final_dir.exists():
                        try:
                            os.replace(trash, final_dir)
                        except OSError:
                            pass
                raise
            for _final_dir, trash in moved:
                self._safe_remove_generated_dir(trash, self._staging_root)
            self._purge_staging_debris(now)
            return removed_count

    # -- database and model helpers -------------------------------------------------

    @staticmethod
    def _ensure_store_directory(path: Path, *, create: bool) -> None:
        if path.is_symlink():
            raise ArtifactError("artifact store directory may not be a symlink")
        if path.exists():
            if not path.is_dir():
                raise ArtifactError("artifact store path is not a directory")
            return
        if not create:
            raise ArtifactError("artifact store directory is missing")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_symlink() or not path.is_dir():
            raise ArtifactError("artifact store directory could not be secured")

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise ArtifactError("artifact store is not initialized")
        return self._conn

    def _require_writable(self) -> None:
        if self.read_only:
            raise ArtifactError("artifact store is read-only")

    @staticmethod
    def _require_owner(owner: ArtifactOwner) -> None:
        if not isinstance(owner, ArtifactOwner):
            raise ArtifactError("owner must be an ArtifactOwner")

    @staticmethod
    def _validate_schema(conn: sqlite3.Connection, *, require_project_versions: bool = True) -> None:
        required_tables = {"artifacts", "artifact_files", "turn_publications"}
        table_names = "'artifacts', 'artifact_files', 'turn_publications', 'project_versions'"
        tables = {
            str(row[0])
            for row in conn.execute(f"SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ({table_names})")
        }
        if require_project_versions:
            required_tables.add("project_versions")
        if not required_tables.issubset(tables):
            raise ArtifactError("artifact store schema is incomplete")
        required_columns = [
            ("artifacts", _REQUIRED_ARTIFACT_COLUMNS),
            ("artifact_files", _REQUIRED_FILE_COLUMNS),
            ("turn_publications", _REQUIRED_TURN_COLUMNS),
        ]
        if "project_versions" in tables:
            required_columns.append(("project_versions", _REQUIRED_PROJECT_VERSION_COLUMNS))
        for table, required in required_columns:
            columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
            if not required.issubset(columns):
                raise ArtifactError(f"artifact store table {table!r} is incomplete")

    @staticmethod
    def _backfill_project_versions(conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            "SELECT project_ref, MAX(version) AS high_water FROM artifacts GROUP BY project_ref"
        ).fetchall()
        for row in rows:
            project_ref = str(row["project_ref"])
            high_water = int(row["high_water"])
            if high_water < 1:
                raise ArtifactError("artifact project version metadata is invalid")
            conn.execute(
                """
                INSERT INTO project_versions (project_ref, high_water)
                VALUES (?, ?)
                ON CONFLICT(project_ref) DO UPDATE SET high_water = MAX(high_water, excluded.high_water)
                """,
                (project_ref, high_water),
            )

    @staticmethod
    def _allocate_project_version(conn: sqlite3.Connection, project_ref: str, *, is_new: bool) -> int:
        if is_new:
            try:
                conn.execute(
                    "INSERT INTO project_versions (project_ref, high_water) VALUES (?, 1)",
                    (project_ref,),
                )
            except sqlite3.IntegrityError as exc:
                raise ArtifactError("artifact project reference is already occupied") from exc
            return 1

        counter = conn.execute(
            "SELECT high_water FROM project_versions WHERE project_ref = ?", (project_ref,)
        ).fetchone()
        if counter is None:
            max_row = conn.execute(
                "SELECT MAX(version) AS version FROM artifacts WHERE project_ref = ?",
                (project_ref,),
            ).fetchone()
            max_version = int(max_row["version"] or 0)
            if max_version < 1:
                raise ArtifactError("artifact project version metadata is missing")
            version = max_version + 1
            if version > 2**63 - 1:
                raise ArtifactError("artifact project version is exhausted")
            conn.execute(
                "INSERT INTO project_versions (project_ref, high_water) VALUES (?, ?)",
                (project_ref, version),
            )
            return version

        high_water = int(counter["high_water"])
        if high_water < 1:
            raise ArtifactError("artifact project version metadata is invalid")
        if high_water >= 2**63 - 1:
            raise ArtifactError("artifact project version is exhausted")
        version = high_water + 1
        conn.execute(
            "UPDATE project_versions SET high_water = ? WHERE project_ref = ?",
            (version, project_ref),
        )
        return version

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _new_ref(prefix: str, pattern: re.Pattern[str]) -> str:
        for _ in range(8):
            value = prefix + secrets.token_urlsafe(32)
            if pattern.fullmatch(value):
                return value
        raise ArtifactError("unable to allocate an opaque artifact reference")

    def _now(self) -> float:
        try:
            value = float(self.clock())
        except (TypeError, ValueError, OverflowError) as exc:
            raise ArtifactError("clock returned an invalid value") from exc
        if not math.isfinite(value):
            raise ArtifactError("clock returned an invalid value")
        return value

    @staticmethod
    def _find_row_by_ref(conn: sqlite3.Connection, ref: str) -> sqlite3.Row:
        if not isinstance(ref, str) or not _ARTIFACT_REF_RE.fullmatch(ref):
            raise ArtifactNotFound("artifact not found")
        row = conn.execute("SELECT * FROM artifacts WHERE artifact_ref = ?", (ref,)).fetchone()
        if row is None:
            raise ArtifactNotFound("artifact not found")
        return row

    def _find_public_row(self, conn: sqlite3.Connection, token: str) -> sqlite3.Row:
        if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
            raise ArtifactNotFound("artifact not found")
        digest = self._token_hash(token)
        row = conn.execute("SELECT * FROM artifacts WHERE token_hash = ?", (digest,)).fetchone()
        if row is None:
            raise ArtifactNotFound("artifact not found")
        stored_token = str(row["token"])
        if not hmac.compare_digest(stored_token, token):
            raise ArtifactNotFound("artifact not found")
        return row

    @staticmethod
    def _authorize_exact_owner(row: sqlite3.Row, owner: ArtifactOwner) -> None:
        if int(row["owner_scope"]) != owner.scope_id or str(row["owner_user"]) != owner.user_id:
            raise ArtifactAccessDenied("artifact is owned by another user")

    @staticmethod
    def _authorize_row(row: sqlite3.Row, owner: ArtifactOwner, *, admin: bool) -> None:
        if int(row["owner_scope"]) != owner.scope_id:
            raise ArtifactAccessDenied("artifact is outside the current scope")
        if admin is not True and str(row["owner_user"]) != owner.user_id:
            raise ArtifactAccessDenied("artifact is owned by another user")

    @staticmethod
    def _require_active(row: sqlite3.Row, now: float) -> None:
        if int(row["revoked"]) or float(row["expires_at"]) <= now:
            raise ArtifactNotFound("artifact not found")

    @staticmethod
    def _artifact_from_row(conn: sqlite3.Connection, row: sqlite3.Row) -> Artifact:
        file_rows = conn.execute(
            "SELECT path, mime, size, sha256, encoding FROM artifact_files WHERE artifact_ref = ? ORDER BY path",
            (row["artifact_ref"],),
        ).fetchall()
        files = tuple(
            ArtifactFileInfo(
                path=str(file_row["path"]),
                mime=str(file_row["mime"]),
                size=int(file_row["size"]),
                sha256=str(file_row["sha256"]),
                encoding=str(file_row["encoding"]),
            )
            for file_row in file_rows
        )
        return Artifact(
            artifact_ref=str(row["artifact_ref"]),
            project_ref=str(row["project_ref"]),
            version=int(row["version"]),
            title=str(row["title"]),
            entry=str(row["entry"]),
            created_at=float(row["created_at"]),
            expires_at=float(row["expires_at"]),
            token=str(row["token"]),
            files=files,
            source_bytes=int(row["source_bytes"]),
            zip_bytes=int(row["zip_bytes"]),
            source_sha256=str(row["source_sha256"]),
        )

    # -- validation and publication -------------------------------------------------

    @staticmethod
    def _merge_files(
        previous: Sequence[ValidatedFile],
        submitted: Sequence[ValidatedFile],
        delete_paths: Sequence[str],
    ) -> tuple[ValidatedFile, ...]:
        merged = {item.path: item for item in previous}
        for path in delete_paths:
            if path in merged:
                del merged[path]
            elif any(path_casefold(path) == path_casefold(existing) for existing in merged):
                raise ArtifactError("delete path does not match the registered spelling")
        existing_by_fold = {path_casefold(path): path for path in merged}
        for item in submitted:
            prior_path = existing_by_fold.get(path_casefold(item.path))
            if prior_path is not None and prior_path != item.path:
                raise ArtifactError("replacement path collides case-insensitively")
            merged[item.path] = item
            existing_by_fold[path_casefold(item.path)] = item.path
        return tuple(sorted(merged.values(), key=lambda item: item.path))

    def _validate_project(self, files: Sequence[ValidatedFile], entry: str) -> None:
        if not files:
            raise ArtifactError("project must contain at least one file")
        if len(files) > self.limits.max_files:
            raise ArtifactLimitError("project exceeds max_files")
        total_bytes = sum(len(item.data) for item in files)
        if total_bytes > self.limits.max_project_bytes:
            raise ArtifactLimitError("project exceeds max_project_bytes")
        if entry not in {item.path for item in files}:
            raise ArtifactError("entry file is not present in the project")
        if not entry.casefold().endswith(".html"):
            raise ArtifactError("entry must be an .html file")
        entry_file = next(item for item in files if item.path == entry)
        if entry_file.mime != "text/html":
            raise ArtifactError("entry must be a real HTML file")

    @staticmethod
    def _build_zip(files: Sequence[ValidatedFile]) -> bytes:
        output = BytesIO()
        with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for item in sorted(files, key=lambda value: value.path):
                info = zipfile.ZipInfo(item.path, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 0
                info.external_attr = 0
                info.comment = b""
                info.extra = b""
                archive.writestr(info, item.data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
        return output.getvalue()

    def _write_staging(self, stage: Path, files: Sequence[ValidatedFile], zip_data: bytes) -> None:
        self._require_generated_directory(self._staging_root, self.root)
        if stage.parent != self._staging_root or stage.exists() or stage.is_symlink():
            raise ArtifactError("invalid artifact staging path")
        stage.mkdir(mode=0o700)
        files_root = stage / "files"
        files_root.mkdir(mode=0o700)
        try:
            for item in files:
                destination = files_root.joinpath(*item.path.split("/"))
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with destination.open("xb") as handle:
                    handle.write(item.data)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    destination.chmod(0o600)
                except OSError:
                    pass
            zip_path = stage / "source.zip"
            with zip_path.open("xb") as handle:
                handle.write(zip_data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                zip_path.chmod(0o600)
                files_root.chmod(0o700)
                stage.chmod(0o700)
            except OSError:
                pass
            self._fsync_directory(files_root)
            self._fsync_directory(stage)
        except BaseException:
            self._safe_remove_generated_dir(stage, self._staging_root)
            raise

    def _enforce_publish_quotas(
        self,
        conn: sqlite3.Connection,
        owner: ArtifactOwner,
        now: float,
        *,
        source_bytes: int,
        zip_bytes: int,
        turn_key: str,
    ) -> None:
        owner_row = conn.execute(
            """SELECT COUNT(*) AS count FROM artifacts
            WHERE owner_scope = ? AND owner_user = ? AND revoked = 0 AND expires_at > ?""",
            (owner.scope_id, owner.user_id, now),
        ).fetchone()
        if int(owner_row["count"]) >= self.limits.max_active_per_owner:
            raise ArtifactLimitError("owner exceeds max_active_per_owner")
        global_row = conn.execute(
            "SELECT COUNT(*) AS count FROM artifacts WHERE revoked = 0 AND expires_at > ?", (now,)
        ).fetchone()
        if int(global_row["count"]) >= self.limits.max_active_global:
            raise ArtifactLimitError("store exceeds max_active_global")
        if turn_key:
            turn_row = conn.execute(
                """
                SELECT publication_count FROM turn_publications
                WHERE owner_scope = ? AND owner_user = ? AND turn_key = ?
                """,
                (owner.scope_id, owner.user_id, turn_key),
            ).fetchone()
            if turn_row is not None and int(turn_row["publication_count"]) >= self.limits.max_per_turn:
                raise ArtifactLimitError("owner exceeds max_per_turn")
        total = self._total_disk_bytes(conn)
        if total + source_bytes + zip_bytes > self.limits.max_total_bytes:
            raise ArtifactLimitError("store exceeds max_total_bytes")

    def _recover_orphan_artifacts(self, conn: sqlite3.Connection) -> None:
        """Remove only unregistered, safe artifact directories from a locked startup scan."""

        managed_seen = 0
        removed = False
        try:
            iterator = self._artifact_root.iterdir()
            for candidate in iterator:
                if candidate.is_symlink():
                    raise ArtifactError("artifact storage path contains a symlink")
                if not _ARTIFACT_REF_RE.fullmatch(candidate.name):
                    continue
                managed_seen += 1
                if managed_seen > _ORPHAN_RECOVERY_BATCH_SIZE:
                    break
                try:
                    mode = candidate.stat().st_mode
                except OSError as exc:
                    raise ArtifactError("artifact storage residue is unavailable") from exc
                if not stat.S_ISDIR(mode):
                    continue
                if (
                    conn.execute("SELECT 1 FROM artifacts WHERE artifact_ref = ?", (candidate.name,)).fetchone()
                    is not None
                ):
                    continue
                if not self._recovery_directory_is_safe(candidate):
                    continue
                self._safe_remove_generated_dir(candidate, self._artifact_root)
                removed = True
        except OSError as exc:
            raise ArtifactError("artifact storage residue is unavailable") from exc
        if removed:
            self._fsync_directory(self._artifact_root)

    @staticmethod
    def _recovery_directory_is_safe(path: Path) -> bool:
        pending = [path]
        inspected = 0
        while pending:
            current = pending.pop()
            if current.is_symlink():
                raise ArtifactError("artifact storage path contains a symlink")
            try:
                current_mode = current.stat().st_mode
            except OSError:
                return False
            if not stat.S_ISDIR(current_mode):
                return False
            try:
                iterator = current.iterdir()
                for child in iterator:
                    inspected += 1
                    if inspected > _RECOVERY_INSPECTION_LIMIT:
                        return False
                    if child.is_symlink():
                        raise ArtifactError("artifact storage path contains a symlink")
                    try:
                        mode = child.stat().st_mode
                    except OSError:
                        return False
                    if stat.S_ISDIR(mode):
                        pending.append(child)
                    elif not stat.S_ISREG(mode):
                        return False
            except OSError:
                return False
        return True

    def _physical_tree_bytes(self, path: Path, *, limit: int) -> int:
        if path.is_symlink():
            raise ArtifactError("artifact storage path contains a symlink")
        try:
            mode = path.stat().st_mode
        except OSError as exc:
            raise ArtifactError("artifact storage residue is unavailable") from exc
        if stat.S_ISREG(mode):
            size = int(path.stat().st_size)
            return size if size <= limit else limit + 1
        if not stat.S_ISDIR(mode):
            raise ArtifactError("artifact storage residue is not regular")

        total = 0
        inspected = 0
        pending = [path]
        while pending:
            current = pending.pop()
            try:
                iterator = current.iterdir()
                for child in iterator:
                    inspected += 1
                    if inspected > _RECOVERY_INSPECTION_LIMIT:
                        return limit + 1
                    if child.is_symlink():
                        raise ArtifactError("artifact storage path contains a symlink")
                    try:
                        child_mode = child.stat().st_mode
                    except OSError as exc:
                        raise ArtifactError("artifact storage residue is unavailable") from exc
                    if stat.S_ISDIR(child_mode):
                        pending.append(child)
                        continue
                    if not stat.S_ISREG(child_mode):
                        raise ArtifactError("artifact storage residue is not regular")
                    total += int(child.stat().st_size)
                    if total > limit:
                        return limit + 1
            except OSError as exc:
                raise ArtifactError("artifact storage residue is unavailable") from exc
        return total

    # -- filesystem integrity -------------------------------------------------------

    def _artifact_dir(self, ref: str) -> Path:
        if not isinstance(ref, str) or not _ARTIFACT_REF_RE.fullmatch(ref):
            raise ArtifactNotFound("artifact not found")
        return self._artifact_root / ref

    @staticmethod
    def _require_generated_directory(path: Path, parent: Path) -> None:
        try:
            path.relative_to(parent)
        except ValueError as exc:
            raise ArtifactError("artifact path escaped its storage root") from exc
        if parent.is_symlink() or path.is_symlink():
            raise ArtifactError("artifact storage path contains a symlink")
        try:
            mode = path.stat().st_mode
        except OSError as exc:
            raise ArtifactNotFound("artifact files are unavailable") from exc
        if not stat.S_ISDIR(mode):
            raise ArtifactError("artifact storage path is not a directory")

    @staticmethod
    def _safe_remove_file(path: Path, parent: Path) -> None:
        try:
            path.relative_to(parent)
        except ValueError:
            return
        try:
            if path.is_symlink():
                path.unlink()
            elif path.exists():
                path.unlink()
        except OSError:
            pass

    @staticmethod
    def _safe_remove_generated_dir(path: Path, parent: Path) -> None:
        try:
            path.relative_to(parent)
        except ValueError:
            return
        try:
            if path.is_symlink():
                path.unlink()
            elif path.exists():
                shutil.rmtree(path)
        except OSError:
            pass

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        try:
            fd = os.open(str(path), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def _file_path(self, row: sqlite3.Row, registered_path: str) -> Path:
        normalized = normalize_artifact_path(registered_path)
        artifact_dir = self._artifact_dir(str(row["artifact_ref"]))
        self._require_generated_directory(artifact_dir, self._artifact_root)
        files_root = artifact_dir / "files"
        self._require_generated_directory(files_root, artifact_dir)
        candidate = files_root.joinpath(*normalized.split("/"))
        try:
            candidate.relative_to(files_root)
        except ValueError as exc:
            raise ArtifactError("artifact file path escaped its storage root") from exc
        current = files_root
        for component in normalized.split("/"):
            current = current / component
            if current.is_symlink():
                raise ArtifactError("artifact file path contains a symlink")
        try:
            mode = candidate.stat().st_mode
        except OSError as exc:
            raise ArtifactNotFound("artifact file is unavailable") from exc
        if not stat.S_ISREG(mode):
            raise ArtifactError("artifact file is not regular")
        return candidate

    def _read_source_file(self, artifact_row: sqlite3.Row, file_row: sqlite3.Row) -> bytes:
        path = str(file_row["path"])
        destination = self._file_path(artifact_row, path)
        expected_size = int(file_row["size"])
        expected_digest = str(file_row["sha256"])
        if expected_size < 0 or expected_size > self.limits.max_file_bytes:
            raise ArtifactError("artifact file size metadata is invalid")
        try:
            if int(destination.stat().st_size) != expected_size:
                raise ArtifactError("artifact file size changed")
        except OSError as exc:
            raise ArtifactNotFound("artifact file is unavailable") from exc
        descriptor = -1
        try:
            flags = os.O_RDONLY
            if hasattr(os, "O_BINARY"):
                flags |= os.O_BINARY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(str(destination), flags)
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                data = handle.read(expected_size + 1)
        except OSError as exc:
            if descriptor >= 0:
                os.close(descriptor)
            raise ArtifactNotFound("artifact file is unavailable") from exc
        if len(data) != expected_size or hashlib.sha256(data).hexdigest() != expected_digest:
            raise ArtifactError("artifact file integrity check failed")
        return data

    def _find_file_row(self, conn: sqlite3.Connection, ref: str, path: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT path, mime, size, sha256, encoding FROM artifact_files WHERE artifact_ref = ? AND path = ?",
            (ref, path),
        ).fetchone()
        if row is None:
            raise ArtifactNotFound("artifact file not found")
        return row

    def _load_validated_files(self, conn: sqlite3.Connection, row: sqlite3.Row) -> tuple[ValidatedFile, ...]:
        source_bytes = int(row["source_bytes"])
        if source_bytes < 0 or source_bytes > self.limits.max_project_bytes:
            raise ArtifactError("artifact source byte accounting failed")
        file_rows = conn.execute(
            "SELECT path, mime, size, sha256, encoding FROM artifact_files WHERE artifact_ref = ? ORDER BY path",
            (row["artifact_ref"],),
        ).fetchall()
        if len(file_rows) > self.limits.max_files:
            raise ArtifactLimitError("artifact manifest exceeds max_files")
        result: list[ValidatedFile] = []
        folded: set[str] = set()
        for file_row in file_rows:
            path = normalize_artifact_path(str(file_row["path"]))
            if path != str(file_row["path"]) or path_casefold(path) in folded:
                raise ArtifactError("artifact manifest contains an invalid path")
            if str(file_row["mime"]) != mime_for_artifact_path(path):
                raise ArtifactError("artifact manifest MIME is invalid")
            mime = str(file_row["mime"])
            encoding = str(file_row["encoding"])
            if encoding not in {"utf-8", "base64"}:
                raise ArtifactError("artifact manifest encoding is invalid")
            folded.add(path_casefold(path))
            data = self._read_source_file(row, file_row)
            if int(file_row["size"]) != len(data):
                raise ArtifactError("artifact manifest size is invalid")
            if mime.startswith("image/") and mime != "image/svg+xml":
                if sniff_image_mime(data) != mime or encoding != "base64":
                    raise ArtifactError("artifact image manifest is invalid")
            else:
                try:
                    data.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ArtifactError("artifact text manifest is invalid") from exc
            result.append(
                ValidatedFile(
                    path=path,
                    data=data,
                    mime=mime,
                    encoding=encoding,
                    sha256=hashlib.sha256(data).hexdigest(),
                )
            )
        files = tuple(result)
        if sum(len(item.data) for item in files) != source_bytes:
            raise ArtifactError("artifact source byte accounting failed")
        if source_digest(files) != str(row["source_sha256"]):
            raise ArtifactError("artifact source integrity check failed")
        return files

    def _read_zip(self, conn: sqlite3.Connection, row: sqlite3.Row) -> bytes:
        files = self._load_validated_files(conn, row)
        expected = self._build_zip(files)
        expected_size = int(row["zip_bytes"])
        if expected_size < 0 or expected_size > self.limits.max_zip_bytes or len(expected) != expected_size:
            raise ArtifactError("artifact ZIP byte accounting failed")
        artifact_dir = self._artifact_dir(str(row["artifact_ref"]))
        self._require_generated_directory(artifact_dir, self._artifact_root)
        archive_path = artifact_dir / "source.zip"
        if archive_path.is_symlink():
            raise ArtifactError("artifact ZIP path contains a symlink")
        try:
            mode = archive_path.stat().st_mode
            actual_size = int(archive_path.stat().st_size)
        except OSError as exc:
            raise ArtifactNotFound("artifact ZIP is unavailable") from exc
        if actual_size != expected_size:
            raise ArtifactError("artifact ZIP size changed")
        if not stat.S_ISREG(mode):
            raise ArtifactError("artifact ZIP is not regular")
        descriptor = -1
        try:
            flags = os.O_RDONLY | (os.O_BINARY if hasattr(os, "O_BINARY") else 0)
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(str(archive_path), flags)
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                data = handle.read(expected_size + 1)
        except OSError as exc:
            if descriptor >= 0:
                os.close(descriptor)
            raise ArtifactNotFound("artifact ZIP is unavailable") from exc
        if data != expected:
            raise ArtifactError("artifact ZIP integrity check failed")
        return data

    def _read_preview(self, row: sqlite3.Row) -> bytes:
        expected_size = int(row["preview_bytes"])
        expected_digest = str(row["preview_sha256"])
        if expected_size <= 0 or expected_size > self.limits.max_preview_bytes or not expected_digest:
            raise ArtifactNotFound("artifact preview not found")
        artifact_dir = self._artifact_dir(str(row["artifact_ref"]))
        self._require_generated_directory(artifact_dir, self._artifact_root)
        path = artifact_dir / "preview.png"
        if path.is_symlink():
            raise ArtifactError("artifact preview path contains a symlink")
        try:
            mode = path.stat().st_mode
            actual_size = int(path.stat().st_size)
        except OSError as exc:
            raise ArtifactNotFound("artifact preview not found") from exc
        if actual_size != expected_size:
            raise ArtifactError("artifact preview size changed")
        if not stat.S_ISREG(mode):
            raise ArtifactError("artifact preview is not regular")
        descriptor = -1
        try:
            flags = os.O_RDONLY | (os.O_BINARY if hasattr(os, "O_BINARY") else 0)
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(str(path), flags)
            with os.fdopen(descriptor, "rb") as handle:
                descriptor = -1
                data = handle.read(expected_size + 1)
        except OSError as exc:
            if descriptor >= 0:
                os.close(descriptor)
            raise ArtifactNotFound("artifact preview not found") from exc
        if len(data) != expected_size or not is_png(data) or hashlib.sha256(data).hexdigest() != expected_digest:
            raise ArtifactError("artifact preview integrity check failed")
        return data

    def _total_disk_bytes(self, conn: sqlite3.Connection) -> int:
        total = 0
        rows = conn.execute("SELECT * FROM artifacts").fetchall()
        registered_refs = {str(row["artifact_ref"]) for row in rows}
        for row in rows:
            artifact_dir = self._artifact_dir(str(row["artifact_ref"]))
            self._require_generated_directory(artifact_dir, self._artifact_root)
            files_root = artifact_dir / "files"
            self._require_generated_directory(files_root, artifact_dir)
            file_rows = conn.execute(
                "SELECT path FROM artifact_files WHERE artifact_ref = ?", (row["artifact_ref"],)
            ).fetchall()
            for file_row in file_rows:
                path = self._file_path(row, str(file_row["path"]))
                total += int(path.stat().st_size)
            archive_path = artifact_dir / "source.zip"
            if archive_path.is_symlink() or not archive_path.is_file():
                raise ArtifactError("artifact ZIP is unavailable")
            total += int(archive_path.stat().st_size)
            preview_path = artifact_dir / "preview.png"
            preview_exists = preview_path.exists() or preview_path.is_symlink()
            if int(row["preview_bytes"]) > 0 and not preview_exists:
                raise ArtifactError("artifact preview is unavailable")
            if preview_exists:
                if preview_path.is_symlink() or not preview_path.is_file():
                    raise ArtifactError("artifact preview is not regular")
                total += int(preview_path.stat().st_size)
            if total > self.limits.max_total_bytes:
                return total

        try:
            for candidate in self._artifact_root.iterdir():
                if candidate.is_symlink():
                    raise ArtifactError("artifact storage path contains a symlink")
                if candidate.name in registered_refs or not _ARTIFACT_REF_RE.fullmatch(candidate.name):
                    continue
                remaining = max(self.limits.max_total_bytes - total, 0)
                total += self._physical_tree_bytes(candidate, limit=remaining)
                if total > self.limits.max_total_bytes:
                    return total
            for candidate in self._staging_root.iterdir():
                if candidate.is_symlink():
                    raise ArtifactError("artifact storage path contains a symlink")
                if not _PURGE_RE.fullmatch(candidate.name):
                    continue
                remaining = max(self.limits.max_total_bytes - total, 0)
                total += self._physical_tree_bytes(candidate, limit=remaining)
                if total > self.limits.max_total_bytes:
                    return total
        except OSError as exc:
            raise ArtifactError("artifact storage residue is unavailable") from exc
        return total

    def _purge_staging_debris(self, now: float) -> None:
        if not self._staging_root.is_dir() or self._staging_root.is_symlink():
            return
        candidates = list(self._staging_root.iterdir())
        removed = 0
        for candidate in candidates:
            if removed >= _PURGE_BATCH_SIZE:
                break
            match = _STAGE_RE.fullmatch(candidate.name) or _PURGE_RE.fullmatch(candidate.name)
            if match is None:
                continue
            created_at = int(match.group(1))
            if created_at > now - _STAGE_MAX_AGE_SECONDS:
                continue
            if candidate.is_symlink() or not candidate.is_dir():
                continue
            self._safe_remove_generated_dir(candidate, self._staging_root)
            removed += 1

    @staticmethod
    def _rollback_transaction(conn: sqlite3.Connection) -> None:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass


__all__ = ["ArtifactStore"]
