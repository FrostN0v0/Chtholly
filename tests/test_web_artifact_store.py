"""Behavioral boundaries for the stdlib web-artifact store."""

from __future__ import annotations

from io import BytesIO
import base64
from pathlib import Path
from zipfile import ZipFile
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from utils.web_artifacts_core import (
    ArtifactError,
    ArtifactOwner,
    ArtifactStore,
    ArtifactLimits,
    ArtifactNotFound,
    ArtifactLimitError,
    ArtifactAccessDenied,
)


class FrozenClock:
    def __init__(self, value: float = 1_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _store(tmp_path: Path, clock: FrozenClock | None = None, **kwargs: object) -> tuple[ArtifactStore, FrozenClock]:
    current_clock = clock or FrozenClock()
    store = ArtifactStore(tmp_path / "artifacts", clock=current_clock, **kwargs)
    store.initialize()
    return store, current_clock


def _files(html: str = "<h1>Hello</h1>") -> list[dict[str, str]]:
    return [
        {"path": "index.html", "content": html},
        {"path": "assets/app.js", "content": "console.log('ok');"},
    ]


def test_publish_reads_registered_files_and_exports_deterministic_zip(tmp_path: Path) -> None:
    store, _clock = _store(tmp_path)
    owner = ArtifactOwner(7, "user-a")

    artifact = store.publish(owner, "Demo", _files())
    data, mime = store.read_owned_file(artifact.artifact_ref, owner, "index.html")
    archive = store.zip_owned(artifact.artifact_ref, owner)

    assert data == b"<h1>Hello</h1>"
    assert mime == "text/html"
    assert len(archive) == artifact.zip_bytes
    with ZipFile(BytesIO(archive)) as zip_file:
        assert zip_file.namelist() == ["assets/app.js", "index.html"]
        assert zip_file.getinfo("index.html").date_time == (1980, 1, 1, 0, 0, 0)
    assert archive == store.zip_public(artifact.token)
    assert artifact.artifact_ref != artifact.token != artifact.project_ref


def test_version_inherits_replaces_and_deletes_without_mutating_previous(tmp_path: Path) -> None:
    store, _clock = _store(tmp_path)
    owner = ArtifactOwner(1, "user")
    first = store.publish(owner, "First", _files())
    second = store.publish(
        owner,
        "Second",
        [{"path": "index.html", "content": "<p>new</p>"}],
        previous_ref=first.artifact_ref,
        delete_paths=["assets/app.js"],
    )

    assert second.project_ref == first.project_ref
    assert second.version == 2
    assert [file.path for file in second.files] == ["index.html"]
    assert store.read_owned_file(first.artifact_ref, owner, "assets/app.js")[0].startswith(b"console")
    with pytest.raises(ArtifactNotFound):
        store.read_owned_file(second.artifact_ref, owner, "assets/app.js")


def test_scope_and_user_ownership_are_enforced_with_same_scope_admin_access(tmp_path: Path) -> None:
    store, _clock = _store(tmp_path)
    owner_a = ArtifactOwner(3, "a")
    owner_b = ArtifactOwner(3, "b")
    other_scope = ArtifactOwner(4, "a")
    artifact = store.publish(owner_a, "Private", _files())

    with pytest.raises(ArtifactAccessDenied):
        store.get_owned(artifact.artifact_ref, owner_b)
    with pytest.raises(ArtifactAccessDenied):
        store.get_owned(artifact.artifact_ref, other_scope, admin=True)
    assert store.get_owned(artifact.artifact_ref, owner_b, admin=True).artifact_ref == artifact.artifact_ref


def test_expiration_and_revoke_remove_every_public_route(tmp_path: Path) -> None:
    store, clock = _store(tmp_path)
    owner = ArtifactOwner(1, "user")
    artifact = store.publish(owner, "Short", _files(), ttl_hours=1)
    assert store.get_public(artifact.token).version == 1

    clock.value += 3_601
    for operation in (
        lambda: store.get_public(artifact.token),
        lambda: store.read_public_file(artifact.token, "index.html"),
        lambda: store.zip_public(artifact.token),
    ):
        with pytest.raises(ArtifactNotFound):
            operation()
    assert store.purge_expired() == 1
    with pytest.raises(ArtifactNotFound):
        store.get_owned(artifact.artifact_ref, owner)

    second = store.publish(owner, "Revocable", _files())
    assert store.revoke(second.artifact_ref, owner) is True
    assert store.revoke(second.artifact_ref, owner) is False
    with pytest.raises(ArtifactNotFound):
        store.get_public(second.token)


def test_paths_casefold_collisions_encoded_traversal_and_image_mismatch_are_rejected(tmp_path: Path) -> None:
    store, _clock = _store(tmp_path)
    owner = ArtifactOwner(1, "user")
    invalid_paths = ("../index.html", "a/../../index.html", "a%2f..%2findex.html", "C:/index.html", "a\\b.html")
    for path in invalid_paths:
        with pytest.raises(ArtifactError):
            store.publish(owner, "Invalid", [{"path": path, "content": "x"}])

    with pytest.raises(ArtifactError):
        store.publish(
            owner,
            "Collision",
            [{"path": "index.html", "content": "a"}, {"path": "INDEX.HTML", "content": "b"}],
        )
    with pytest.raises(ArtifactError):
        store.publish(
            owner,
            "Image",
            [
                {"path": "index.html", "content": "ok"},
                {
                    "path": "image.png",
                    "content": base64.b64encode(b"not-a-png").decode("ascii"),
                    "encoding": "base64",
                },
            ],
        )


def test_turn_quota_persists_and_failed_quota_publication_leaves_no_artifact(tmp_path: Path) -> None:
    clock = FrozenClock()
    root = tmp_path / "artifacts"
    limits = ArtifactLimits(max_per_turn=1)
    store = ArtifactStore(root, limits=limits, clock=clock).initialize()
    owner = ArtifactOwner(1, "user")
    first = store.publish(owner, "First", _files(), turn_key="turn-1")
    with pytest.raises(ArtifactLimitError):
        store.publish(owner, "Second", _files(), turn_key="turn-1")
    assert len(store.list_owned(owner)) == 1

    store.close()
    reopened = ArtifactStore(root, limits=limits, clock=clock).initialize()
    with pytest.raises(ArtifactLimitError):
        reopened.publish(owner, "Again", _files(), turn_key="turn-1")
    assert reopened.get_owned(first.artifact_ref, owner).version == 1


def test_concurrent_publications_share_persisted_turn_quota(tmp_path: Path) -> None:
    store, _clock = _store(tmp_path, limits=ArtifactLimits(max_per_turn=1))
    owner = ArtifactOwner(1, "user")
    barrier = threading.Barrier(2)

    def attempt(title: str) -> str:
        barrier.wait()
        try:
            store.publish(owner, title, _files(), turn_key="same-turn")
        except ArtifactLimitError:
            return "limited"
        return "published"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = sorted(executor.map(attempt, ("A", "B")))
    assert results == ["limited", "published"]
    assert len(store.list_owned(owner)) == 1


def test_read_only_store_does_not_create_or_mutate_catalog(tmp_path: Path) -> None:
    writable, clock = _store(tmp_path)
    owner = ArtifactOwner(1, "user")
    artifact = writable.publish(owner, "Read only", _files())
    writable.close()
    root = tmp_path / "artifacts"
    read_only = ArtifactStore(root, clock=clock, read_only=True).initialize()

    assert read_only.get_public(artifact.token).artifact_ref == artifact.artifact_ref
    with pytest.raises(ArtifactError):
        read_only.publish(owner, "Nope", _files())
    with pytest.raises(ArtifactError):
        read_only.attach_preview(artifact.artifact_ref, owner, b"\x89PNG\r\n\x1a\n")
    assert not (root / "artifacts.sqlite3-wal").exists()
    assert not (root / "artifacts.sqlite3-shm").exists()


def test_preview_is_single_immutable_png_and_counts_toward_total_quota(tmp_path: Path) -> None:
    store, clock = _store(tmp_path, limits=ArtifactLimits(max_preview_bytes=64, max_total_bytes=1_000_000))
    owner = ArtifactOwner(1, "user")
    artifact = store.publish(owner, "Preview", _files())
    png = b"\x89PNG\r\n\x1a\n" + b"derivative"

    store.close()
    tight_limit = artifact.source_bytes + artifact.zip_bytes + len(png) - 1
    tight = ArtifactStore(
        store.root, limits=ArtifactLimits(max_preview_bytes=64, max_total_bytes=tight_limit), clock=clock
    )
    tight.initialize()
    with pytest.raises(ArtifactLimitError):
        tight.attach_preview(artifact.artifact_ref, owner, png)
    tight.close()

    room = ArtifactStore(
        store.root,
        limits=ArtifactLimits(
            max_preview_bytes=64, max_total_bytes=artifact.source_bytes + artifact.zip_bytes + len(png)
        ),
        clock=clock,
    ).initialize()
    room.attach_preview(artifact.artifact_ref, owner, png)
    assert room.preview_public(artifact.token) == png
    with pytest.raises(ArtifactError):
        room.attach_preview(artifact.artifact_ref, owner, png)


def test_atomic_publication_compensates_when_staging_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, _clock = _store(tmp_path)
    owner = ArtifactOwner(1, "user")

    def fail_staging(stage: Path, files: object, zip_data: bytes) -> None:
        stage.mkdir()
        (stage / "partial").write_bytes(b"partial")
        raise KeyboardInterrupt

    monkeypatch.setattr(store, "_write_staging", fail_staging)
    with pytest.raises(KeyboardInterrupt):
        store.publish(owner, "Interrupted", _files())
    assert store.list_owned(owner) == []
    assert list((store.root / ".staging").iterdir()) == []


def test_utf8_byte_limit_is_enforced_after_encoding(tmp_path: Path) -> None:
    store, _clock = _store(tmp_path, limits=ArtifactLimits(max_file_bytes=4))
    owner = ArtifactOwner(1, "user")

    with pytest.raises(ArtifactError, match="too large"):
        store.publish(owner, "Multibyte", [{"path": "index.html", "content": "ééé"}])


def test_revoke_purge_then_branching_keeps_project_version_high_water(tmp_path: Path) -> None:
    store, _clock = _store(tmp_path)
    owner = ArtifactOwner(1, "user")
    first = store.publish(owner, "First", _files())
    second = store.publish(owner, "Second", _files(), previous_ref=first.artifact_ref)

    assert second.version == 2
    assert store.revoke(second.artifact_ref, owner) is True
    assert store.purge_expired() == 1

    third = store.publish(owner, "Third", _files(), previous_ref=first.artifact_ref)
    assert third.project_ref == first.project_ref
    assert third.version == 3


def test_initialize_recovers_unregistered_final_directory_after_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, clock = _store(tmp_path)
    owner = ArtifactOwner(1, "user")

    def crash(_token: str) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr(store, "_token_hash", crash)
    monkeypatch.setattr(store, "_safe_remove_generated_dir", lambda _path, _parent: None)
    with pytest.raises(KeyboardInterrupt):
        store.publish(owner, "Interrupted", _files())

    orphan_dirs = [path for path in (store.root / "artifacts").iterdir() if path.name.startswith("wa_")]
    assert len(orphan_dirs) == 1
    orphan_dir = orphan_dirs[0]
    store.close()

    reopened = ArtifactStore(store.root, clock=clock).initialize()
    assert not orphan_dir.exists()
    assert reopened.list_owned(owner) == []
