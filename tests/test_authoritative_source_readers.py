"""Authoritative source reads preserve bytes and enforce storage ownership across turns."""

from __future__ import annotations

from io import BytesIO
import json
from typing import Any, cast
from hashlib import sha256
from zipfile import ZipFile
from collections.abc import Callable, Awaitable

import pytest

from plugins.llm_chat.tools import (
    read_web_artifact as artifact_read_module,
    read_workshop_plugin as workshop_read_module,
    list_workshop_plugins as workshop_list_module,
)
from utils.web_artifacts_core import ArtifactOwner, ArtifactStore
from plugins.llm_chat.agent_context import AgentAccessContext, agent_access_scope
from plugins.llm_chat.core.delivery import DeliveryError, DeliveryState, llm_chat_delivery_scope
from plugins.plugin_workshop.config import WorkshopConfig
from plugins.plugin_workshop.service import PluginWorkshopService
from plugins.llm_chat.tools._workshop import WorkshopToolContext, authorized_workshop_actor
from utils.plugin_workshop_core.codec import manifest_payload
from utils.plugin_workshop_core.store import WorkshopStore
from plugins.llm_chat.tools._artifacts import ArtifactToolContext
from utils.plugin_workshop_core.models import Actor, CheckResult, WorkshopError, ValidationReport
from utils.plugin_workshop_core.policy import canonical_json, parse_manifest
from plugins.llm_chat.artifacts_runtime import WebArtifactService

SOURCE = "# exact unicode and mixed newlines\r\nVALUE = '雪'\n# " + "x" * 17000 + "\r\n"
AUXILIARY = "unused = 'keep me'\r\n"


def declaration():
    return parse_manifest(
        {
            "title": "Repair",
            "description": "Read before revision",
            "commands": ["repair"],
            "permissions": [],
            "data_description": "None",
            "configuration": {"color": "雪"},
            "checks": [{"command": "repair", "expected_contains": "ok"}],
        }
    )


class NoSandbox:
    async def status(self):
        raise AssertionError("Source reads must not contact the sandbox")

    async def validate(self, request):
        raise AssertionError("Source reads must not execute candidates")

    async def aclose(self):
        pass


@pytest.fixture
def workshop(tmp_path, monkeypatch):
    store = WorkshopStore(tmp_path)
    store.initialize()
    service = PluginWorkshopService(WorkshopConfig(), root=tmp_path, store=store, sandbox=NoSandbox())
    # No plugin lifecycle is involved in read-only tests; the real store/service do all reads.
    service._ready = True
    runtime = WorkshopToolContext(lambda: service, lambda message: None)
    monkeypatch.setattr(workshop_read_module, "register_tool", lambda dispatcher, function: function)
    monkeypatch.setattr(workshop_list_module, "register_tool", lambda dispatcher, function: function)
    return (
        store,
        workshop_read_module.register_read_workshop_plugin(cast(Any, None), runtime),
        workshop_list_module.register_list_workshop_plugins(cast(Any, None), runtime),
    )


def test_scope_is_applied_before_revision_pagination_and_global_admin_is_unchanged(tmp_path):
    store = WorkshopStore(tmp_path)
    store.initialize()
    own = Actor("alice", 1)
    for name, actor in (("aaa_foreign", Actor("alice", 2)), ("bbb_other", Actor("bob", 1)), ("ccc_own", own)):
        store.create_version(name, {"__init__.py": "VALUE = 1\n"}, declaration(), actor)
    store.create_version("ccc_own", {"__init__.py": "VALUE = 2\n"}, declaration(), own)
    assert [(r.plugin_name, r.version) for r in store.list_revisions(own, query_scope=1, limit=1)] == [("ccc_own", 2)]
    assert [r.version for r in store.list_revisions(own, query_scope=1, limit=1, offset=1)] == [1]
    admin = Actor("operator", 1, True)
    assert {r.plugin_name for r in store.list_revisions(admin, query_scope=1)} == {"bbb_other", "ccc_own"}
    with pytest.raises(WorkshopError):
        store.read_files("aaa_foreign", 1, admin, query_scope=1)
    with pytest.raises(WorkshopError):
        store.list_revisions(admin, query_scope=2)
    # The authenticated WebUI intentionally retains its existing global administrative interface.
    assert len(store.list_projects(admin)) == 3
    assert store.read_files("aaa_foreign", 1, admin) == {"__init__.py": "VALUE = 1\n"}


@pytest.mark.asyncio
async def test_workshop_next_turn_exact_source_manifest_and_failure_report(workshop):
    store, read, listing = workshop
    with agent_access_scope(AgentAccessContext(1, 2, 3, "alice")), llm_chat_delivery_scope(DeliveryState()):
        actor, _ = authorized_workshop_actor()
        first = store.create_version("repair", {"__init__.py": SOURCE, "unused.py": AUXILIARY}, declaration(), actor)
    log = "acceptance\r\n" + "雪" * 18000
    store.mark_validating("repair", first.version)
    store.record_report(
        "repair",
        first.version,
        ValidationReport("failed", first.source_hash, (CheckResult("output", False, "missing"),), log),
    )
    with agent_access_scope(AgentAccessContext(1, 20, 30, "alice")), llm_chat_delivery_scope(DeliveryState()):
        versions = json.loads(await listing())
        assert versions["plugins"][0]["source_hash"] == first.source_hash
        page = json.loads(await read("repair", 1, mode="file", path="__init__.py", max_chars=100000))
        assert len(page["content"]) == 16000
        final = json.loads(await read("repair", 1, mode="file", path="__init__.py", offset=page["next_offset"]))
        assert (page["content"] + final["content"]).encode() == SOURCE.encode()
        manifest = json.loads(await read("repair", 1, mode="manifest"))
        assert manifest["content"].encode() == canonical_json(manifest_payload(first.manifest))
        files = json.loads(await read("repair", 1, mode="files", limit=1))
        more = json.loads(await read("repair", 1, mode="files", offset=files["next_offset"], limit=1))
        assert more["files"] == [
            {
                "path": "unused.py",
                "mime": "text/x-python",
                "size": len(AUXILIARY.encode()),
                "sha256": sha256(AUXILIARY.encode()).hexdigest(),
            }
        ]
        report = json.loads(await read("repair", 1, mode="report"))
        report_tail = json.loads(
            await read(
                "repair", 1, mode="report", offset=report["next_offset"], content_sha256=report["content_sha256"]
            )
        )
        assert json.loads(report["content"] + report_tail["content"])["checks"][0]["passed"] is False
        log_page = json.loads(await read("repair", 1, mode="log"))
        with pytest.raises(DeliveryError):
            await read("repair", 1, mode="log", offset=log_page["next_offset"])
        log_tail = json.loads(
            await read(
                "repair", 1, mode="log", offset=log_page["next_offset"], content_sha256=log_page["content_sha256"]
            )
        )
        assert log_page["content"] + log_tail["content"] == log
        old_files = store.read_files("repair", 1, actor)
        second = store.create_version("repair", {**old_files, "__init__.py": "VALUE = 2\n"}, first.manifest, actor)
        assert store.read_files("repair", second.version, actor)["unused.py"].encode() == AUXILIARY.encode()
        assert json.loads(await read("repair", 1, mode="file", path="unused.py"))["content"] == AUXILIARY
        assert store.get_version("repair", 1, actor).approved_by is None
        assert store.active_versions() == store.pending_operations() == []
        store.mark_validating("repair", 1)
        store.record_report("repair", 1, ValidationReport("failed", first.source_hash, log="new run"))
        with pytest.raises(DeliveryError):
            await read(
                "repair", 1, mode="log", offset=log_page["next_offset"], content_sha256=log_page["content_sha256"]
            )


@pytest.mark.asyncio
async def test_workshop_tool_rejects_other_owners_paths_and_tampered_bytes(workshop, tmp_path):
    store, read, listing = workshop
    with agent_access_scope(AgentAccessContext(1, 2, 3, "alice")), llm_chat_delivery_scope(DeliveryState()):
        actor, _ = authorized_workshop_actor()
        store.create_version("repair", {"__init__.py": "VALUE = 1\r\n"}, declaration(), actor)
        for path in ("../__init__.py", "/__init__.py", "missing.py"):
            with pytest.raises(DeliveryError):
                await read("repair", 1, mode="file", path=path)
    with agent_access_scope(AgentAccessContext(1, 2, 4, "bob")), llm_chat_delivery_scope(DeliveryState()):
        assert json.loads(await listing())["plugins"] == []
        with pytest.raises(DeliveryError):
            await read("repair", 1)
    next(tmp_path.rglob("__init__.py")).write_bytes(b"VALUE = 9\r\n")
    with agent_access_scope(AgentAccessContext(1, 2, 5, "alice")), llm_chat_delivery_scope(DeliveryState()):
        for mode in ("metadata", "manifest", "file", "report"):
            with pytest.raises(DeliveryError):
                await read("repair", 1, mode=mode, path="__init__.py" if mode == "file" else "")


@pytest.mark.asyncio
async def test_artifact_manifest_discovers_unused_files_and_pins_exact_zip_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(artifact_read_module, "register_tool", lambda dispatcher, function: function)
    service = WebArtifactService(tmp_path, public_origin="https://preview.example")
    read = cast(
        Callable[..., Awaitable[str]],
        artifact_read_module.register_read_web_artifact(
            cast(Any, None), ArtifactToolContext(service, lambda message: None)
        ),
    )
    owner = ArtifactOwner(1, "alice")
    unused = "const value = '雪';\r\n" + "// aux\n" * 3000
    try:
        first = await service.publish(
            owner, "Page", [{"path": "index.html", "content": "<p>one</p>"}, {"path": "unused.js", "content": unused}]
        )
        second = await service.publish(
            owner, "Page", [{"path": "unused.js", "content": "// changed"}], previous_ref=first.artifact_ref
        )
        with agent_access_scope(AgentAccessContext(1, 2, 3, "alice")), llm_chat_delivery_scope(DeliveryState()):
            page = json.loads(await read(None, first.artifact_ref, mode="manifest", limit=1))
            final = json.loads(
                await read(None, first.artifact_ref, mode="manifest", limit=1, offset=page["next_offset"])
            )
            assert [item["path"] for item in page["files"] + final["files"]] == ["index.html", "unused.js"]
            assert final["files"][0] == {
                "path": "unused.js",
                "mime": "text/javascript",
                "size": len(unused.encode()),
                "sha256": sha256(unused.encode()).hexdigest(),
                "entry": False,
            }
            contents = []
            offset = 0
            while offset is not None:
                result = json.loads(await read(None, first.artifact_ref, mode="file", path="unused.js", offset=offset))
                contents.append(result["content"])
                offset = result["next_offset"]
            assert "".join(contents).encode() == unused.encode()
            with ZipFile(BytesIO(service.store.zip_owned(first.artifact_ref, owner))) as archive:
                assert archive.read("unused.js") == "".join(contents).encode()
            assert (
                json.loads(await read(None, second.artifact_ref, mode="file", path="unused.js"))["content"]
                == "// changed"
            )
            with pytest.raises(DeliveryError):
                await read(None, first.artifact_ref, mode="file", path="../workshop.sqlite3")
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_artifact_read_modes_recheck_ownership_revocation_expiry_and_integrity(tmp_path, monkeypatch):
    monkeypatch.setattr(artifact_read_module, "register_tool", lambda dispatcher, function: function)
    clock = [1000.0]
    store = ArtifactStore(tmp_path, clock=lambda: clock[0])
    service = WebArtifactService(tmp_path, public_origin="https://preview.example", store=store)
    read = cast(
        Callable[..., Awaitable[str]],
        artifact_read_module.register_read_web_artifact(
            cast(Any, None), ArtifactToolContext(service, lambda message: None)
        ),
    )
    owner = ArtifactOwner(1, "alice")
    try:
        artifact = await service.publish(owner, "Page", [{"path": "index.html", "content": "<p>one</p>"}], ttl_hours=1)
        for access in (AgentAccessContext(1, 2, 3, "bob"), AgentAccessContext(2, 2, 3, "alice", is_operator=True)):
            with agent_access_scope(access), llm_chat_delivery_scope(DeliveryState()):
                for mode in ("manifest", "file"):
                    with pytest.raises(DeliveryError):
                        await read(None, artifact.artifact_ref, mode=mode)
        with (
            agent_access_scope(AgentAccessContext(1, 2, 3, "bob", is_operator=True)),
            llm_chat_delivery_scope(DeliveryState()),
        ):
            assert json.loads(await read(None, artifact.artifact_ref, mode="manifest"))["version"] == 1
        with agent_access_scope(AgentAccessContext(1, 2, 3, "alice")), llm_chat_delivery_scope(DeliveryState()):
            next(tmp_path.rglob("index.html")).write_bytes(b"<p>two</p>")
            with pytest.raises(DeliveryError):
                await read(None, artifact.artifact_ref, mode="file")
            clock[0] += 3601
            for mode in ("manifest", "file"):
                with pytest.raises(DeliveryError):
                    await read(None, artifact.artifact_ref, mode=mode)
            fresh = await service.publish(owner, "Page", [{"path": "index.html", "content": "<p>ok</p>"}])
            store.revoke(fresh.artifact_ref, owner)
            for mode in ("manifest", "file"):
                with pytest.raises(DeliveryError):
                    await read(None, fresh.artifact_ref, mode=mode)
    finally:
        await service.close()
