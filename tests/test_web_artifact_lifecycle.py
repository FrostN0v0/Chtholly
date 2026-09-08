"""Lifecycle boundaries for the managed web-artifact service."""

from __future__ import annotations

from typing import cast
import asyncio
from pathlib import Path
import threading

import pytest
from launart import Launart

from utils.web_artifacts_core import Artifact, ArtifactOwner, ArtifactStore, ArtifactFileInfo
from plugins.llm_chat.artifacts_runtime import WebArtifactService

_TOKEN = "wt_" + "t" * 32
_ARTIFACT_REF = "wa_" + "a" * 32
_PROJECT_REF = "wp_" + "p" * 32


def _artifact() -> Artifact:
    source = b"<html><body>demo</body></html>"
    return Artifact(
        artifact_ref=_ARTIFACT_REF,
        project_ref=_PROJECT_REF,
        version=1,
        title="Demo",
        entry="index.html",
        created_at=100.0,
        expires_at=200.0,
        token=_TOKEN,
        files=(ArtifactFileInfo("index.html", "text/html", len(source), "x" * 64, "utf-8"),),
        source_bytes=len(source),
        zip_bytes=128,
        source_sha256="y" * 64,
    )


class _LifecycleStore:
    def __init__(self) -> None:
        self.initialize_started = threading.Event()
        self.initialize_release = threading.Event()
        self.publish_started = threading.Event()
        self.publish_release = threading.Event()
        self.revoke_started = threading.Event()
        self.revoke_release = threading.Event()
        self.initialize_count = 0
        self.purge_count = 0
        self.purge_after_close = 0
        self.revoked: list[str] = []
        self.closed = False

    def initialize(self) -> _LifecycleStore:
        self.initialize_count += 1
        self.initialize_started.set()
        if not self.initialize_release.wait(timeout=5):
            raise RuntimeError("initialize test gate timed out")
        return self

    def purge_expired(self) -> int:
        self.purge_count += 1
        if self.closed:
            self.purge_after_close += 1
        return 0

    def publish(self, *_args: object, **_kwargs: object) -> Artifact:
        self.publish_started.set()
        if not self.publish_release.wait(timeout=5):
            raise RuntimeError("publish test gate timed out")
        return _artifact()

    def revoke(self, ref: str, _owner: ArtifactOwner, *, admin: bool = False) -> bool:
        del admin
        self.revoke_started.set()
        if not self.revoke_release.wait(timeout=5):
            raise RuntimeError("revoke test gate timed out")
        self.revoked.append(ref)
        return True

    def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_registered_service_prepares_store_and_purges_without_tool_call(tmp_path: Path) -> None:
    store = _LifecycleStore()
    service = WebArtifactService(
        tmp_path,
        public_origin="https://preview.example",
        store=cast(ArtifactStore, store),
        purge_interval_seconds=60.0,
    )
    manager = Launart()
    manager.add_component(service)
    launch_task = asyncio.create_task(manager.launch())
    try:
        await asyncio.to_thread(store.initialize_started.wait, 5)
        store.initialize_release.set()
        for _ in range(100):
            if service.status.prepared:
                break
            await asyncio.sleep(0.01)
        assert service.status.prepared
        assert store.initialize_count == 1
        assert store.purge_count == 1
        assert service._purge_task is not None
        manager.status.exiting = True
        await launch_task
    finally:
        store.initialize_release.set()
        if not launch_task.done():
            manager.status.exiting = True
            await launch_task
    assert store.closed


@pytest.mark.asyncio
async def test_initialize_close_race_does_not_purge_after_store_close(tmp_path: Path) -> None:
    store = _LifecycleStore()
    service = WebArtifactService(
        tmp_path,
        public_origin="https://preview.example",
        store=cast(ArtifactStore, store),
        purge_interval_seconds=60.0,
    )
    start_task = asyncio.create_task(service._ensure_started())
    await asyncio.to_thread(store.initialize_started.wait, 5)
    close_task = asyncio.create_task(service.close())
    await asyncio.sleep(0)
    assert not close_task.done()
    assert not store.closed
    store.initialize_release.set()
    await start_task
    await close_task
    assert store.closed
    assert store.purge_after_close == 0


@pytest.mark.asyncio
async def test_cancelled_publish_joins_double_cancellation_and_compensation_before_close(tmp_path: Path) -> None:
    store = _LifecycleStore()
    service = WebArtifactService(
        tmp_path,
        public_origin="https://preview.example",
        store=cast(ArtifactStore, store),
    )
    owner = ArtifactOwner(scope_id=1, user_id="user-a")
    publish_task = asyncio.create_task(
        service.publish(
            owner,
            "Demo",
            [{"path": "index.html", "content": "<html><body>demo</body></html>"}],
            turn_key="turn-1",
        )
    )
    store.initialize_release.set()
    await asyncio.to_thread(store.publish_started.wait, 5)
    publish_task.cancel()
    store.publish_release.set()
    await asyncio.to_thread(store.revoke_started.wait, 5)
    publish_task.cancel()
    store.revoke_release.set()
    with pytest.raises(asyncio.CancelledError):
        await publish_task
    assert store.revoked == [_ARTIFACT_REF]
    assert not store.closed
    await service.close()
    assert store.closed


__all__ = []
