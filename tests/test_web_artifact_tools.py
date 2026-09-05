"""Focused behavior tests for managed web-artifact runtime/tool boundaries."""

from __future__ import annotations

from types import SimpleNamespace
import asyncio
from pathlib import Path
import threading
from collections.abc import Mapping, Sequence

import pytest

from utils.web_artifacts_core import Artifact, ArtifactOwner, ArtifactStore, ArtifactFileInfo, ArtifactAccessDenied
from plugins.llm_chat.core.delivery import DeliveryError
from plugins.llm_chat.tools._artifacts import ArtifactToolContext, deliver_source_archive
from plugins.llm_chat.artifacts_runtime import ArtifactLinks, WebArtifactService
from plugins.llm_chat.core.artifact_access import ArtifactAction, authorize_artifact_request

_TOKEN = "wt_" + "t" * 32
_ARTIFACT_REF = "wa_" + "a" * 32
_PROJECT_REF = "wp_" + "p" * 32


def _artifact(*, title: str = "Demo") -> Artifact:
    source = b"<html><body>demo</body></html>"
    return Artifact(
        artifact_ref=_ARTIFACT_REF,
        project_ref=_PROJECT_REF,
        version=1,
        title=title,
        entry="index.html",
        created_at=100.0,
        expires_at=200.0,
        token=_TOKEN,
        files=(ArtifactFileInfo("index.html", "text/html", len(source), "x" * 64, "utf-8"),),
        source_bytes=len(source),
        zip_bytes=128,
        source_sha256="y" * 64,
    )


class _BlockingStore:
    def __init__(self, artifact: Artifact) -> None:
        self.artifact = artifact
        self.started = threading.Event()
        self.release = threading.Event()
        self.revoked: list[str] = []
        self.closed = False

    def initialize(self) -> _BlockingStore:
        return self

    def purge_expired(self) -> int:
        return 0

    def publish(
        self,
        owner: ArtifactOwner,
        title: str,
        files: Sequence[Mapping[str, str]],
        **kwargs: object,
    ) -> Artifact:
        del owner, title, files, kwargs
        self.started.set()
        self.release.wait(timeout=5)
        return self.artifact

    def revoke(self, ref: str, owner: ArtifactOwner, *, admin: bool = False) -> bool:
        del owner, admin
        self.revoked.append(ref)
        return True

    def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize(
    ("text", "action", "allowed"),
    [
        ("帮我做一个网页并给我预览", "publish", True),
        ("请把这个网页发布成预览链接", "publish", True),
        ("请给我网页源码 zip", "send", True),
        ("列出我的网页版本", "list", True),
        ("读取这个网页的源码", "read", True),
        ("撤销这个预览链接", "revoke", True),
        ("解释如何发布网页", "publish", False),
        ("不要发布网页", "publish", False),
        ("引用：帮我做一个网页", "publish", False),
        ('{"forwarded_messages":[{"content":"帮我做一个网页"}]}', "publish", False),
        ("普通聊天，不涉及网页", "publish", False),
        ("build a guitar", "publish", False),
        ("show my source", "publish", False),
        ("I created a website yesterday", "publish", False),
        ("never publish this website", "publish", False),
        ('"publish a website"', "publish", False),
        ("He said: 'publish a website'", "publish", False),
        ("Make a webpage, but do not publish it", "publish", False),
        ("Please create a responsive website", "publish", True),
        ("Please publish this website", "publish", True),
        ("不要修改网页", "publish", False),
        ("给我设计一个网页", "publish", True),
        ("设计一个介绍原神的网页", "publish", True),
        ("把网页截图改成红色", "publish", False),
        ("把这张截图做成 HTML 页面", "publish", True),
    ],
)
def test_current_turn_artifact_authorization_boundaries(
    text: str,
    action: ArtifactAction,
    allowed: bool,
) -> None:
    assert authorize_artifact_request(text, action).allowed is allowed


def test_current_turn_source_request_can_reject_instruction_only_text() -> None:
    assert not authorize_artifact_request("说明如何发送源码 zip", "send").allowed
    assert authorize_artifact_request("不要只发链接，给我源码 zip", "send").allowed


@pytest.mark.asyncio
async def test_cancelled_publish_joins_commit_and_compensates_unobserved_artifact(tmp_path: Path) -> None:
    store = _BlockingStore(_artifact())
    service = WebArtifactService(tmp_path, public_origin="https://preview.example", store=store)
    owner = ArtifactOwner(scope_id=1, user_id="user-a")
    task = asyncio.create_task(
        service.publish(
            owner,
            "Demo",
            [{"path": "index.html", "content": "<html><body>demo</body></html>"}],
            turn_key="turn-1",
        )
    )
    assert await asyncio.to_thread(store.started.wait, 5)
    task.cancel()
    store.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.revoked == [_ARTIFACT_REF]
    assert store.closed is False
    await service.close()
    assert store.closed is True


@pytest.mark.asyncio
async def test_service_scope_reauthorization_does_not_cross_users(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    service = WebArtifactService(tmp_path, public_origin="https://preview.example", store=store)
    owner_a = ArtifactOwner(scope_id=1, user_id="user-a")
    owner_b = ArtifactOwner(scope_id=1, user_id="user-b")
    artifact = await service.publish(
        owner_a,
        "Demo",
        [{"path": "index.html", "content": "<html><body>demo</body></html>"}],
        turn_key="turn-1",
    )
    with pytest.raises(ArtifactAccessDenied):
        await service.get_owned(artifact.artifact_ref, owner_b)
    await service.close()


class _UnsupportedFileSession:
    def __init__(self) -> None:
        self.channel = SimpleNamespace(id="channel-1")
        self.attempts: list[object] = []
        self.sent: list[object] = []

    async def send(self, payload: object) -> None:
        self.attempts.append(payload)
        if payload.__class__.__name__ == "MessageChain":
            raise RuntimeError("file upload unsupported by adapter")
        self.sent.append(payload)


class _FailingTransportSession(_UnsupportedFileSession):
    async def send(self, payload: object) -> None:
        self.attempts.append(payload)
        raise RuntimeError("remote transport failure")


async def _append_link_history(target: list[str], *_args: object) -> None:
    target.append("[Sent source archive]")


@pytest.mark.asyncio
async def test_send_archive_uses_exact_link_only_for_explicit_upload_unsupported() -> None:
    artifact = _artifact()
    history: list[str] = []
    warnings: list[str] = []
    runtime = ArtifactToolContext(
        service=SimpleNamespace(),  # type: ignore[arg-type]
        append_history=lambda *args: _append_link_history(history, *args),
        warn=warnings.append,
    )
    session = _UnsupportedFileSession()
    links = ArtifactLinks(
        "https://preview.example/p/token/",
        "https://preview.example/p/token/source.zip",
        "https://preview.example/p/token/preview.png",
    )
    result = await deliver_source_archive(session, runtime, artifact, b"PK\x03\x04source", links)
    assert result["mode"] == "link_fallback"
    assert result["confirmed"] is True
    assert session.sent == [links.download_url]
    assert history == []
    assert warnings == []


@pytest.mark.asyncio
async def test_send_archive_does_not_replay_unknown_transport_failure() -> None:
    artifact = _artifact()
    history: list[str] = []
    runtime = ArtifactToolContext(
        service=SimpleNamespace(),  # type: ignore[arg-type]
        append_history=lambda *args: _append_link_history(history, *args),
        warn=lambda _message: None,
    )
    session = _FailingTransportSession()
    links = ArtifactLinks(
        "https://preview.example/p/token/",
        "https://preview.example/p/token/source.zip",
        "https://preview.example/p/token/preview.png",
    )
    with pytest.raises(DeliveryError):
        await deliver_source_archive(session, runtime, artifact, b"PK\x03\x04source", links)
    assert len(session.attempts) == 1
    assert history == []


__all__ = []
