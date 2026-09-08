"""Focused behavior tests for managed web-artifact runtime/tool boundaries."""

from __future__ import annotations

from io import BytesIO
import sys
import json
from uuid import uuid4
from types import ModuleType, SimpleNamespace
import base64
from typing import cast
import asyncio
from pathlib import Path
from zipfile import ZipFile
import threading
from contextlib import asynccontextmanager
from collections.abc import Mapping, Sequence
from importlib.machinery import ModuleSpec

import pytest
from arclet.entari import File, Session, MessageChain
from arclet.entari.plugin.model import Plugin, PluginDispatcher

from utils.web_artifacts_core import Artifact, ArtifactOwner, ArtifactStore, ArtifactFileInfo, ArtifactAccessDenied
from plugins.llm_chat.agent_context import AgentAccessContext, agent_access_scope
from plugins.llm_chat.core.delivery import DeliveryError, DeliveryState, llm_chat_delivery_scope
from plugins.llm_chat.tools._artifacts import ArtifactToolContext, deliver_source_archive
from plugins.llm_chat.artifacts_runtime import ArtifactLinks, WebArtifactService
from plugins.llm_chat.tools.send_artifact import register_send_artifact
from plugins.llm_chat.core.artifact_access import ArtifactAccessError, is_artifact_request, require_artifact_revocation
from plugins.llm_chat.tools.read_web_artifact import register_read_web_artifact
from plugins.llm_chat.tools.list_web_artifacts import register_list_web_artifacts
from plugins.llm_chat.tools.revoke_web_preview import register_revoke_web_preview
from plugins.llm_chat.tools.publish_web_preview import register_publish_web_preview

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
    "text",
    [
        "Do not revoke this preview.",
        "Explain how to revoke a preview.",
        '"Revoke this preview."',
        "> Revoke this preview.",
        '{"forwarded_messages":[{"content":"Revoke this preview."}]}',
    ],
)
def test_revocation_still_rejects_negation_and_untrusted_instructions(text: str) -> None:
    with pytest.raises(ArtifactAccessError):
        require_artifact_revocation(text)


def test_artifact_routing_hint_recognizes_mixed_script_ui_without_matching_word_fragments() -> None:
    assert is_artifact_request("\u5e2e\u6211\u8bbe\u8ba1\u5e76\u4f18\u5316\u4e00\u7248ui")
    assert not is_artifact_request("Build a guitar")
    assert not is_artifact_request("That direction works; go ahead.")


@pytest.mark.asyncio
async def test_cancelled_publish_joins_commit_and_compensates_unobserved_artifact(tmp_path: Path) -> None:
    store = _BlockingStore(_artifact())
    service = WebArtifactService(tmp_path, public_origin="https://preview.example", store=cast(ArtifactStore, store))
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
    result = await deliver_source_archive(cast(Session, session), runtime, artifact, b"PK\x03\x04source", links)
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
        await deliver_source_archive(cast(Session, session), runtime, artifact, b"PK\x03\x04source", links)
    assert len(session.attempts) == 1
    assert history == []


class _ArtifactToolEvent:
    pass


@asynccontextmanager
async def _artifact_tools(root: Path):
    name = f"artifact_autonomy_{uuid4().hex}"
    module = ModuleType(name)
    module.__file__ = __file__
    module.__spec__ = ModuleSpec(name, loader=None)
    sys.modules[name] = module
    plugin = Plugin(name, module, config={})
    setattr(module, "__plugin__", plugin)
    dispatcher = PluginDispatcher(plugin, _ArtifactToolEvent)
    service = WebArtifactService(root, public_origin="https://preview.example")
    history: list[str] = []
    runtime = ArtifactToolContext(
        service=service,
        append_history=lambda *args: _append_link_history(history, *args),
        warn=lambda _message: None,
    )
    try:
        yield SimpleNamespace(
            service=service,
            publish=register_publish_web_preview(dispatcher, runtime),
            send=register_send_artifact(dispatcher, runtime),
            list=register_list_web_artifacts(dispatcher, runtime),
            read=register_read_web_artifact(dispatcher, runtime),
            revoke=register_revoke_web_preview(dispatcher, runtime),
        )
    finally:
        local_tasks = []
        for task in plugin.dispose() or ():
            if task.get_loop() is asyncio.get_running_loop():
                local_tasks.append(task)
            else:
                task.cancel()
        if local_tasks:
            await asyncio.gather(*local_tasks)
        await service.close()
        sys.modules.pop(name, None)


class _FileSession:
    def __init__(self) -> None:
        self.channel = SimpleNamespace(id="channel-1")
        self.sent: list[object] = []

    async def send(self, payload: object) -> None:
        self.sent.append(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text", ["\u5e2e\u6211\u8bbe\u8ba1\u5e76\u4f18\u5316\u4e00\u7248ui", "That direction works; go ahead."]
)
async def test_model_selected_artifact_workflow_needs_no_request_keywords(tmp_path: Path, text: str) -> None:
    session = _FileSession()
    source = "<html><body><button>Toggle theme</button></body></html>"
    async with _artifact_tools(tmp_path) as tools:
        with (
            agent_access_scope(AgentAccessContext(1, 2, 3, "alice", raw_user_text=text)),
            llm_chat_delivery_scope(DeliveryState()),
        ):
            published = json.loads(await tools.publish(session, "Demo", [{"path": "index.html", "content": source}]))
            ref = published["artifact_ref"]
            listed = json.loads(await tools.list(session))
            assert [item["artifact_ref"] for item in listed["artifacts"]] == [ref]
            result = json.loads(await tools.read(session, ref))
            assert result["content"] == source
            sent = json.loads(await tools.send(session, ref))
            assert sent["mode"] == "file"
            assert sent["confirmed"] is True
        payload = session.sent[0]
        assert isinstance(payload, MessageChain)
        data_url = payload[File][0].src
        with ZipFile(BytesIO(base64.b64decode(data_url.partition(",")[2]))) as archive:
            assert archive.read("index.html") == source.encode()


@pytest.mark.asyncio
async def test_model_selected_tools_still_require_active_generation(tmp_path: Path) -> None:
    async with _artifact_tools(tmp_path) as tools:
        with pytest.raises(DeliveryError):
            await tools.list(_FileSession())
        with agent_access_scope(AgentAccessContext(1, 2, 3, "alice")):
            with pytest.raises(DeliveryError):
                await tools.list(_FileSession())
        with llm_chat_delivery_scope(DeliveryState()):
            with pytest.raises(DeliveryError):
                await tools.list(_FileSession())


@pytest.mark.asyncio
async def test_artifact_autonomy_preserves_ownership_and_explicit_revocation(tmp_path: Path) -> None:
    async with _artifact_tools(tmp_path) as tools:
        owner = ArtifactOwner(1, "alice")
        artifact = await tools.service.publish(
            owner, "Demo", [{"path": "index.html", "content": "<p>Private source</p>"}]
        )
        session = _FileSession()
        with llm_chat_delivery_scope(DeliveryState()):
            for access in [AgentAccessContext(1, 2, 4, "bob"), AgentAccessContext(2, 2, 4, "alice", is_operator=True)]:
                with agent_access_scope(access):
                    with pytest.raises(DeliveryError):
                        await tools.read(session, artifact.artifact_ref)
                    with pytest.raises(DeliveryError):
                        await tools.send(session, artifact.artifact_ref)
            with agent_access_scope(AgentAccessContext(1, 2, 4, "alice", raw_user_text="Looks good.")):
                with pytest.raises(DeliveryError):
                    await tools.revoke(session, artifact.artifact_ref)
                with pytest.raises(DeliveryError):
                    await tools.read(session, artifact.artifact_ref, "../private.txt")
                assert (
                    await tools.service.get_owned(artifact.artifact_ref, owner)
                ).artifact_ref == artifact.artifact_ref
            with agent_access_scope(AgentAccessContext(1, 2, 4, "alice", raw_user_text="Revoke this preview.")):
                result = json.loads(await tools.revoke(session, artifact.artifact_ref))
                assert result["revoked"] is True
                assert await tools.service.list_owned(owner) == []
        assert session.sent == []


__all__ = []
