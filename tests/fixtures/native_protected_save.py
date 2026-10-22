"""Exercise protected saves in a disposable, account-free native Entari process."""

from __future__ import annotations

import os
import sys
from copy import deepcopy
import json
from types import SimpleNamespace
from typing import Any
import asyncio
from hashlib import sha256
from pathlib import Path
from functools import partial
from contextlib import contextmanager
from dataclasses import field

import pytest

PROBE = """import os
import asyncio
from pathlib import Path
from arclet.entari import BasicConfModel, ConfigReload, command, listen, metadata, plugin_config

class Config(BasicConfModel):
    message: str = "stable"
    retries: int = 1
    api_key: str | None = None

metadata(name="Protected native save probe", config=Config)
cfg = plugin_config(Config)
started = asyncio.Event()
release = asyncio.Event()

if cfg.message == "reject":
    Path("rejected-candidate.txt").write_text("reached plugin import", encoding="utf-8")
    raise RuntimeError("private candidate failure: " + os.environ["NATIVE_SAVE_KEY"])

@command.on("protected_save_probe")
def respond():
    return f"{cfg.message}|{cfg.retries}"

@listen(ConfigReload)
async def wait_for_observation(event: ConfigReload):
    if event.scope == "plugin" and event.key == "probe" and event.value.get("message") == "waiting":
        started.set()
        await release.wait()
"""

DISPOSABLE = """from arclet.entari import BasicConfModel, command, metadata, plugin_config
class Config(BasicConfModel):
    message: str = "disposable"
metadata(name="Partial application probe", config=Config)
cfg = plugin_config(Config)
@command.on("disposable_save_probe")
def respond():
    return cfg.message
"""

WORKSHOP = """from arclet.entari import BasicConfModel, command, metadata, plugin_config
class Config(BasicConfModel):
    message: str = "workshop-stable"
metadata(name="Workshop ownership probe", config=Config)
cfg = plugin_config(Config)
@command.on("workshop_save_probe")
def respond():
    return cfg.message
"""


def bootstrap(*, yaml_comments: bool = False):
    from arclet.entari import Entari

    config = {
        "basic": {
            "network": [],
            "external_dirs": [str(Path.cwd())],
            "log": {"level": "ERROR", "rich_error": False, "save": False},
            "schema": False,
        },
        "plugins": {
            "$prefix": [{"key": "", "plugins": ["probe", "disposable", "workshop_echo", "workshop_idle"]}],
            ".localdata": {"app_name": "protected-native-save"},
            "database": {"name": str(Path("database.sqlite3").resolve())},
            "server": {"host": "127.0.0.1", "port": 0, "path": "satori", "adapters": []},
            "entari_plugin_webui": {},
            "probe": {"message": "initial", "remove_me": 7, "api_key": "${{ env.NATIVE_SAVE_KEY }}"},
            "disposable": {},
            "workshop_echo": {"message": "workshop-stable"},
            "workshop_idle": {"$optional": True},
            "llm": {"$optional": True},
            "llm_chat": {"$optional": True, "companion_daily_contacts": 1},
            "webui_sso": {"$optional": True},
            "webui_config_apply": {"$optional": True},
            "plugins.webui_config_apply": {},
        },
    }
    config_path = Path("entari.yml" if yaml_comments else "entari.json")
    if yaml_comments:
        from io import StringIO

        from ruamel.yaml import YAML

        yaml = YAML(typ="rt")
        yaml.preserve_quotes = True
        stream = StringIO()
        yaml.dump(config, stream)
        document = yaml.load(stream.getvalue())
        document.yaml_set_start_comment("Operator-maintained native save configuration")
        document["basic"]["log"].yaml_add_eol_comment("Keep the operator log setting", "level")
        document["plugins"].yaml_set_comment_before_after_key(
            "probe", before="Keep this plugin in its original position", indent=2
        )
        document["plugins"]["probe"] = yaml.load("""message: "initial" # Keep the message inline documentation
remove_me: 7
retries: 3 # This setting will return to its default
api_key: '${{ env.NATIVE_SAVE_KEY }}' # Keep the environment reference documentation
notes: | # Keep the multiline documentation
  first operator note
  second operator note
""")
        document["plugins"]["probe"].yaml_set_comment_before_after_key(
            "message", before="Keep the message setting documentation", indent=4
        )
        with config_path.open("w", encoding="utf-8") as stream:
            yaml.dump(document, stream)
    else:
        config_path.write_text(json.dumps(config), encoding="utf-8")
    for filename, source in (
        ("probe.py", PROBE),
        ("disposable.py", DISPOSABLE),
        ("workshop_echo.py", WORKSHOP),
        ("workshop_idle.py", WORKSHOP),
    ):
        Path(filename).write_text(source, encoding="utf-8")
    app = Entari.load(str(config_path))
    return app


@contextmanager
def model_environment():
    from arclet.entari import BasicConfModel
    from arclet.entari.config import EntariConfig, config_model_validate

    from plugins.webui_config_apply import saving
    from plugins.webui_config_apply.model_reload import ModelReloadBindings, prepare_model_reload

    class Model(BasicConfModel):
        name: str
        alias: str | None = None
        api_key: str | None = None
        base_url: str = "https://api.openai.com/v1"
        prompt: str = ""
        extra: dict[str, Any] = field(default_factory=dict)

    class Config(BasicConfModel):
        api_key: str | None = None
        base_url: str = "https://api.openai.com/v1"
        prompt: str = ""
        models: list[Model] = field(default_factory=list)
        tools: dict[str, dict[str, Any]] = field(default_factory=dict)

    # ModelReload resolves annotations through the defining module, not this scope.
    globals()["Model"] = Model
    path = Path("entari.yml").resolve()
    path.write_text(
        'basic:\n  log:\n    level: "info"\nplugins:\n'
        '  llm:\n    models:\n      - name: "old-model"\n'
        '  other:\n    title: "quoted"\n    notes: |\n      first line\n      second line\n    ratio: 0.5\n',
        encoding="utf-8",
    )
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(EntariConfig, "instance", EntariConfig.instance)
        monkeypatch.setattr(EntariConfig, "_inited", False)
        config = EntariConfig(path, env_vars={})
        runtime = config_model_validate(Config, {"models": [{"name": "old-model"}]})
        plug = SimpleNamespace(config=config.plugin["llm"], _is_disposed=False)
        state_path = Path("state.json").resolve()
        state_path.write_text(json.dumps({"$default": {"default_model": "old-model"}}), encoding="utf-8")
        bindings = ModelReloadBindings("llm", plug, runtime, Config, config_model_validate, state_path)
        monkeypatch.setattr(saving, "prepare_model_reload", partial(prepare_model_reload, bindings=bindings))
        saver = saving.ConfigSaver(sha256(path.read_bytes()).hexdigest(), restart_available=lambda: False)
        yield SimpleNamespace(saver=saver, path=path, state_path=state_path, runtime=runtime, monkeypatch=monkeypatch)


async def model_case(case: str, value: object) -> None:
    from arclet.entari.config import EntariConfig
    from arclet.entari.config.model import ApplyReport
    from entari_plugin_webui.services.config_service import get_etag

    from utils.webui_config_core import ConfigValidationError, validate_candidate
    from plugins.webui_config_apply import saving

    with model_environment() as env:
        if case == "model-save":
            original_inode = env.path.stat().st_ino
            report = await env.saver.save_section(
                "plugins:llm",
                {"models": [{"name": "new-model"}]},
                if_match=get_etag(),
            )
            assert isinstance(report, ApplyReport)
            assert report.ok
            assert report.applied
            assert not report.errors
            assert report.changed_plugins == ["llm"]
            assert env.saver.application_mode == "hot_reload"
            assert env.saver.last_application["result"] == "applied"
            assert env.saver.running_sha256 == sha256(env.path.read_bytes()).hexdigest()
            assert env.path.stat().st_ino == original_inode
            assert json.loads(env.state_path.read_text())["$default"]["default_model"] == "new-model"
            assert env.runtime.models[0].name == "new-model"
            unchanged = env.path.read_bytes()
            repeated = await env.saver.save_section(
                "plugins:llm",
                {"models": [{"name": "new-model"}]},
                if_match=get_etag(),
            )
            assert repeated.ok
            assert not repeated.applied
            assert not repeated.errors
            assert env.path.read_bytes() == unchanged
            assert env.runtime.models[0].name == "new-model"
            return

        original = env.path.read_bytes()
        inode = env.path.stat().st_ino
        running = env.saver.running_sha256
        if case == "state-rollback":
            env.state_path.write_bytes(b"{unreadable-state")
            with pytest.raises(ConfigValidationError) as caught:
                await env.saver.save_section(
                    "plugins:llm",
                    {"models": [{"name": "new-model"}]},
                    if_match=get_etag(),
                )
            assert caught.value.code == "model_state_failed"
            assert "unreadable-state" not in str(caught.value)
            assert env.state_path.read_bytes() == b"{unreadable-state"
        elif case == "close-rollback":
            state = env.state_path.read_bytes()
            original_open = Path.open
            fail_close = True

            @contextmanager
            def closing_failure(stream):
                with stream:
                    yield stream
                raise OSError("fixture close failure")

            def open_file(path, mode="r", *args, **kwargs):
                nonlocal fail_close
                stream = original_open(path, mode, *args, **kwargs)
                if path == env.path and mode == "r+b" and fail_close:
                    fail_close = False
                    return closing_failure(stream)
                return stream

            env.monkeypatch.setattr(Path, "open", open_file)
            with pytest.raises(saving.SaveError):
                await env.saver.save_section(
                    "plugins:llm",
                    {"models": [{"name": "new-model"}]},
                    if_match=get_etag(),
                )
            assert fail_close is False
            assert env.state_path.read_bytes() == state
        elif case == "invalid-budget":
            with pytest.raises(ConfigValidationError) as caught:
                await env.saver.save_section(
                    "plugins:llm_chat",
                    {"companion_daily_contacts": value},
                    if_match=get_etag(),
                )
            assert caught.value.path == "config.plugins.llm_chat.companion_daily_contacts"
            assert "llm_chat" not in EntariConfig.instance.plugin
        elif case == "operator-budget":
            env.saver.restart_available = lambda: True
            chosen = {
                "companion_daily_contacts": 96,
                "companion_daily_reflections": 48,
                "companion_reflection_interval_minutes": 15,
            }
            desired = deepcopy(EntariConfig.instance.plugin)
            desired["llm_chat"] = chosen
            # No llm_chat plugin is loaded here: a foundational entry deliberately
            # makes this a restart save instead of attempting an unrelated import.
            desired["server"] = {"host": "127.0.0.1", "port": 0}
            report = await env.saver.save_section("plugins", desired, if_match=get_etag())
            assert report.ok
            assert not report.applied
            assert report.ignored
            assert validate_candidate(env.path.read_bytes(), {})["plugins"]["llm_chat"] == chosen
            assert env.saver.application_mode == "restart"
            assert env.saver.last_application["result"] == "pending"
            assert env.saver.last_application["mode"] == "restart"
            assert env.saver.running_sha256 == running
            assert env.runtime.models[0].name == "old-model"
            return
        else:
            raise AssertionError(f"Unknown model scenario: {case}")
        assert env.path.read_bytes() == original
        assert env.path.stat().st_ino == inode
        assert env.saver.running_sha256 == running
        assert env.runtime.models[0].name == "old-model"


async def native_case(case: str) -> None:
    import httpx
    from arclet.entari import command
    from entari_plugin_webui import app, _session_store
    from arclet.entari.config import EntariConfig
    from arclet.entari.plugin import find_plugin
    from entari_plugin_webui.services import config_service
    from entari_plugin_webui.core.security import set_local_mode

    from plugins.webui_config_apply.api import install_api

    path = EntariConfig.instance.path
    initial = path.read_bytes()
    initial_inode = path.stat().st_ino
    initial_digest = sha256(initial).hexdigest()
    set_local_mode(False)
    cookie = _session_store.create(ip="isolated protected-save consumer")
    dispose = install_api(app, initial_digest)
    origin = {"Origin": "http://testserver"}
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", cookies={"webui_sid": cookie}
    ) as client:

        async def status():
            response = await client.get("/api/config-apply/status")
            assert response.status_code == 200, response.text
            return response.json()

        async def save(section, data, *, plugin_route=False, headers=None):
            route = f"/api/plugins/{section}/config" if plugin_route else f"/api/config/{section}"
            field_name = "config" if plugin_route else "data"
            request_headers = {**origin, "If-Match": config_service.get_etag(), **(headers or {})}
            return await client.put(route, json={field_name: data}, headers=request_headers)

        async def unchanged():
            assert path.read_bytes() == initial
            assert path.stat().st_ino == initial_inode
            assert await command.execute("protected_save_probe") == "initial|1"
            assert await command.execute("workshop_save_probe") == "workshop-stable"
            current = await status()
            assert current["running_sha256"] == initial_digest
            assert current["in_sync"] is True

        try:
            expected_initial = "initial|3" if case == "native-yaml-comments" else "initial|1"
            assert await command.execute("protected_save_probe") == expected_initial
            if case == "native-etag":
                etag = config_service.get_etag()
                for route, field_name in (
                    ("/api/config/plugins:probe", "data"),
                    ("/api/plugins/probe/config", "config"),
                ):
                    for if_match, expected in ((None, 428), ("outdated", 409)):
                        headers = dict(origin)
                        if if_match is not None:
                            headers["If-Match"] = if_match
                        response = await client.put(route, json={field_name: {"message": "not-saved"}}, headers=headers)
                        assert response.status_code == expected, response.text
                        assert response.json()["etag"] == etag
                        await unchanged()
                response = await save("probe", {"message": "accepted"}, plugin_route=True)
                assert response.status_code == 200, response.text
                assert response.json()["apply"]["ok"], response.text
                assert await command.execute("protected_save_probe") == "accepted|1"
                stale = await save("plugins:probe", {"message": "stale"}, headers={"If-Match": etag})
                assert stale.status_code == 409, stale.text
                assert await command.execute("protected_save_probe") == "accepted|1"
            elif case == "native-bool-number":
                before = config_service.get_section("plugins:llm_chat")
                assert type(before["companion_daily_contacts"]) is int
                assert before["companion_daily_contacts"] == 1
                response = await save("plugins:llm_chat", {**before, "companion_daily_contacts": True})
                assert response.status_code == 400, response.text
                await unchanged()
                assert type(config_service.get_section("plugins:llm_chat")["companion_daily_contacts"]) is int
            elif case == "native-yaml-comments":
                from ruamel.yaml import YAML

                yaml = YAML(typ="rt")
                yaml.preserve_quotes = True
                initial_text = initial.decode("utf-8")
                original = yaml.load(initial_text)
                comments = (
                    "Operator-maintained native save configuration",
                    "Keep the operator log setting",
                    "Keep this plugin in its original position",
                    "Keep the message setting documentation",
                    "Keep the message inline documentation",
                    "This setting will return to its default",
                    "Keep the environment reference documentation",
                    "Keep the multiline documentation",
                )
                for comment in comments:
                    assert initial_text.count(comment) == 1, ("Fixture omitted authored comment", comment, initial_text)
                desired = {
                    "message": "stable",
                    "retries": 1,
                    "api_key": os.environ["NATIVE_SAVE_KEY"],
                    "notes": "first operator note\nsecond operator note\n",
                }
                response = await save("probe", desired, plugin_route=True)
                assert response.status_code == 200, response.text
                assert response.json()["apply"]["ok"], response.text
                persisted_text = path.read_text(encoding="utf-8")
                persisted = yaml.load(persisted_text)
                probe = persisted["plugins"]["probe"]
                assert list(persisted) == list(original)
                assert list(persisted["plugins"]) == list(original["plugins"])
                assert list(probe) == ["message", "retries", "api_key", "notes"]
                assert probe["message"] == "stable"
                assert probe["retries"] == 1
                assert "remove_me" not in probe
                assert probe["api_key"] == original["plugins"]["probe"]["api_key"]
                assert probe["notes"] == desired["notes"]
                assert probe["message"].style == '"'
                assert probe["api_key"].style == "'"
                assert probe["notes"].style == "|"
                for comment in comments:
                    assert persisted_text.count(comment) == 1, (comment, persisted_text)
                assert os.environ["NATIVE_SAVE_KEY"] not in persisted_text
                assert path.stat().st_ino == initial_inode
                assert await command.execute("protected_save_probe") == "stable|1"
                current = await status()
                assert current["in_sync"] is True
                assert current["last_application"]["result"] == "applied"
            elif case == "native-deletion-defaults":
                response = await save("probe", {"message": "saved", "retries": 1}, plugin_route=True)
                assert response.status_code == 200, response.text
                assert response.json()["apply"]["ok"], response.text
                persisted = json.loads(path.read_text(encoding="utf-8"))["plugins"]["probe"]
                assert persisted == {"message": "saved"}, persisted
                assert await command.execute("protected_save_probe") == "saved|1"
                response = await save("plugins:probe", {})
                assert response.status_code == 200, response.text
                assert response.json()["apply"]["ok"], response.text
                assert json.loads(path.read_text(encoding="utf-8"))["plugins"]["probe"] == {}
                assert await command.execute("protected_save_probe") == "stable|1"
            elif case == "native-section-deletion":
                desired = json.loads(path.read_text(encoding="utf-8"))["plugins"]
                desired.pop("disposable")
                response = await save("plugins", desired)
                assert response.status_code == 200, response.text
                report = response.json()["apply"]
                assert report["ok"], report
                assert "disposable" in report["unloaded"], report
                assert "disposable" not in json.loads(path.read_text(encoding="utf-8"))["plugins"]
                assert find_plugin("disposable") is None
                assert await command.execute("disposable_save_probe") is None
                assert await command.execute("protected_save_probe") == "initial|1"
            elif case == "native-credential-reference":
                reference = json.loads(path.read_text(encoding="utf-8"))["plugins"]["probe"]["api_key"]
                response = await client.get("/api/config/plugins:probe")
                assert response.status_code == 200, response.text
                desired = response.json()["data"]
                desired.update(message="credential-preserved", api_key=os.environ["NATIVE_SAVE_KEY"])
                response = await save("probe", desired, plugin_route=True)
                assert response.status_code == 200, response.text
                assert response.json()["apply"]["ok"], response.text
                persisted = json.loads(path.read_text(encoding="utf-8"))["plugins"]["probe"]
                assert persisted["api_key"] == reference
                assert os.environ["NATIVE_SAVE_KEY"].encode() not in path.read_bytes()
                assert find_plugin("probe").module.cfg.api_key == os.environ["NATIVE_SAVE_KEY"]
                assert await command.execute("protected_save_probe") == "credential-preserved|1"
            elif case == "native-apply":
                old_module = find_plugin("probe").module
                task = asyncio.create_task(save("plugins:probe", {"message": "waiting"}))
                try:
                    await asyncio.wait_for(old_module.started.wait(), 5)
                    during = await asyncio.wait_for(status(), 3)
                    assert during["last_application"]["result"] == "applying", during
                    assert during["last_application"]["mode"] == "hot_reload", during
                    assert during["running_sha256"] == initial_digest
                    assert during["saved_sha256"] == sha256(path.read_bytes()).hexdigest()
                    assert during["last_application"]["candidate_sha256"] == during["saved_sha256"]
                    assert during["in_sync"] is False
                    assert await command.execute("protected_save_probe") == "initial|1"
                finally:
                    old_module.release.set()
                    if not task.done():
                        response = await asyncio.wait_for(task, 5)
                    else:
                        response = task.result()
                assert response.status_code == 200, response.text
                report = response.json()["apply"]
                assert report["ok"], report
                assert report["applied"], report
                assert "probe" in report["reloaded"], report
                assert await command.execute("protected_save_probe") == "waiting|1"
                assert path.stat().st_ino == initial_inode
                current = await status()
                assert current["last_application"]["result"] == "applied"
                assert current["in_sync"] is True
                assert current["running_sha256"] == sha256(path.read_bytes()).hexdigest()
            elif case == "native-partial-candidate":
                original_plugins = json.loads(initial.decode("utf-8"))["plugins"]
                desired = deepcopy(original_plugins)
                desired["disposable"]["message"] = "partial-accepted"
                desired["probe"]["message"] = "reject"
                assert await command.execute("disposable_save_probe") == "disposable"
                response = await save("plugins", desired)
                assert response.status_code == 200, response.text
                report = response.json()["apply"]
                assert report["ok"] is False, report
                assert response.json()["success"] is False, report
                assert "disposable" in report["reloaded"], report
                assert {error["plugin"] for error in report["errors"]} == {"probe"}, report
                assert Path("rejected-candidate.txt").read_text(encoding="utf-8") == "reached plugin import"
                assert await command.execute("disposable_save_probe") == "partial-accepted"
                assert await command.execute("protected_save_probe") == "initial|1"
                candidate = path.read_bytes()
                persisted = json.loads(candidate.decode("utf-8"))["plugins"]
                assert persisted["disposable"]["message"] == "partial-accepted"
                assert persisted["probe"]["message"] == "reject"
                assert path.stat().st_ino == initial_inode
                current = await status()
                assert current["last_application"]["result"] == "failed"
                assert current["last_application"]["mode"] == "hot_reload"
                assert current["running_sha256"] is None
                assert current["in_sync"] is False
                assert current["saved_sha256"] == sha256(candidate).hexdigest()
                assert current["last_application"]["candidate_sha256"] == current["saved_sha256"]
                assert current["available"] is False
                restored = await save("plugins", original_plugins)
                assert restored.status_code == 503, restored.text
                assert restored.json()["code"] == "helper_unavailable", restored.text
                assert path.read_bytes() == candidate
                assert path.stat().st_ino == initial_inode
                assert await command.execute("disposable_save_probe") == "partial-accepted"
                assert await command.execute("protected_save_probe") == "initial|1"
                current = await status()
                assert current["running_sha256"] is None
                assert current["in_sync"] is False
                assert current["last_application"]["result"] == "failed"
            elif case == "native-failed-candidate":
                response = await save("probe", {"message": "reject"}, plugin_route=True)
                assert response.status_code == 200, response.text
                report = response.json()["apply"]
                assert report["ok"] is False, report
                assert report["errors"], report
                assert response.json()["success"] is False
                assert Path("rejected-candidate.txt").read_text(encoding="utf-8") == "reached plugin import"
                assert "private candidate failure" not in response.text
                assert os.environ["NATIVE_SAVE_KEY"] not in response.text
                assert json.loads(path.read_text(encoding="utf-8"))["plugins"]["probe"] == {"message": "reject"}
                assert path.stat().st_ino == initial_inode
                assert await command.execute("protected_save_probe") == "initial|1"
                current = await status()
                assert current["last_application"]["result"] == "failed"
                assert current["running_sha256"] == initial_digest
                assert current["saved_sha256"] == sha256(path.read_bytes()).hexdigest() != initial_digest
                assert current["last_application"]["candidate_sha256"] == current["saved_sha256"]
                assert current["in_sync"] is False
            elif case == "native-protected-lifecycle":
                requests = [
                    ("PUT", "/api/plugins/workshop_echo/config", {"config": {"message": "forbidden"}}, 403),
                    ("PUT", "/api/config/plugins:workshop_echo", {"data": {"message": "forbidden"}}, 403),
                    ("POST", "/api/plugins/load", {"key": "workshop_idle"}, 403),
                ]
                for action in ("toggle", "reload", "unload", "uninstall"):
                    requests.append(("POST", f"/api/plugins/workshop_echo/{action}", {"enable": False}, 403))
                for plugin_id in ("server", "database", "entari_plugin_webui"):
                    for action in ("toggle", "reload", "unload", "uninstall"):
                        requests.append(("POST", f"/api/plugins/{plugin_id}/{action}", {"enable": False}, 409))
                for plugin_id in ("llm", "webui_sso", "webui_config_apply"):
                    requests.append(("POST", "/api/plugins/load", {"key": plugin_id}, 409))
                for method, route, body, expected in requests:
                    response = await client.request(
                        method, route, json=body, headers={**origin, "If-Match": config_service.get_etag()}
                    )
                    assert response.status_code == expected, (route, response.status_code, response.text)
                    assert os.environ["NATIVE_SAVE_KEY"] not in response.text
                    await unchanged()
                    assert find_plugin("workshop_idle") is None
                    assert all(
                        find_plugin(plugin_id) is not None
                        for plugin_id in ("server", "database", "entari_plugin_webui")
                    )
            elif case == "native-origin":
                for headers in (
                    {"X-Requested-With": "XMLHttpRequest"},
                    {"Origin": "http://outside.invalid", "X-Requested-With": "XMLHttpRequest"},
                    {"Origin": "https://testserver", "X-Requested-With": "XMLHttpRequest"},
                ):
                    for method, route, body in (
                        ("PUT", "/api/config/plugins:probe", {"data": {"message": "outside"}}),
                        ("PUT", "/api/plugins/probe/config", {"config": {"message": "outside"}}),
                        ("POST", "/api/plugins/probe/toggle", {"enable": False}),
                        ("POST", "/api/plugins/probe/reload", {}),
                        ("POST", "/api/config-apply/restart", {}),
                    ):
                        response = await client.request(
                            method, route, json=body, headers={**headers, "If-Match": config_service.get_etag()}
                        )
                        assert response.status_code == 403, (route, response.status_code, response.text)
                        await unchanged()
                async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as unauthenticated:
                    response = await unauthenticated.put(
                        "/api/config/plugins:probe",
                        json={"data": {"message": "anonymous"}},
                        headers={**origin, "If-Match": config_service.get_etag()},
                    )
                    assert response.status_code == 401, response.text
                await unchanged()
                response = await client.put(
                    "/api/config/plugins:probe",
                    json={"data": {"message": "referred"}},
                    headers={"Referer": "http://testserver/config", "If-Match": config_service.get_etag()},
                )
                assert response.status_code == 200, response.text
                assert response.json()["apply"]["ok"], response.text
                assert await command.execute("protected_save_probe") == "referred|1"
            elif case == "native-dispose":
                dispose()
                response = await client.put(
                    "/api/config/plugins:probe",
                    json={"data": {"message": "native-restored"}},
                    headers={"X-Requested-With": "XMLHttpRequest", "If-Match": config_service.get_etag()},
                )
                assert response.status_code == 200, response.text
                assert response.json()["apply"]["ok"], response.text
                assert await command.execute("protected_save_probe") == "native-restored|1"
            elif case == "native-dispose-ownership":
                second_dispose = install_api(app, initial_digest)
                try:
                    dispose()
                    response = await client.put(
                        "/api/config/plugins:probe",
                        json={"data": {"message": "not-saved"}},
                        headers={"X-Requested-With": "XMLHttpRequest", "If-Match": config_service.get_etag()},
                    )
                    assert response.status_code == 403, response.text
                    response = await save("plugins:probe", {"message": "second-owner"})
                    assert response.status_code == 200, response.text
                    assert response.json()["apply"]["ok"], response.text
                    assert await command.execute("protected_save_probe") == "second-owner|1"
                finally:
                    second_dispose()
            else:
                raise AssertionError(f"Unknown native scenario: {case}")
        finally:
            dispose()


async def main() -> None:
    from creart import it
    from launart import Launart
    from arclet.entari import AiohttpClientService
    from arclet.entari.plugin import find_plugin
    from arclet.letoderea.scope import Scope, _scopes
    from arclet.letoderea.utils import set_event_loop
    from arclet.entari.plugin.service import plugin_service

    set_event_loop(asyncio.get_running_loop())
    root_scope = Scope.root()
    case = sys.argv[2]
    manager = it(Launart)
    running = None
    try:
        app = bootstrap(yaml_comments=case == "native-yaml-comments")
        manager.add_component(AiohttpClientService())
        # add_component invokes Entari.ensure_manager, which loads every
        # configured plugin in native order and installs the plugin manager.
        manager.add_component(app)
        running = asyncio.create_task(manager.launch())
        await asyncio.wait_for(plugin_service.status.wait_for("blocking"), 15)
        assert find_plugin("probe") is not None
        assert find_plugin("workshop_echo") is not None
        assert find_plugin("workshop_idle") is None
        guard = find_plugin("plugins.webui_config_apply")
        assert guard is not None
        # Import the guard only through Entari's loader. Remove its initial
        # collected API/dumper effects so each scenario can own install_api and
        # its status routes without an older saver shadowing the new one.
        await guard._scope.cleanup()
        if case.startswith("native-"):
            await native_case(case)
        else:
            await model_case(case, json.loads(sys.argv[3]))
    finally:
        try:
            if running is not None:
                manager.status.exiting = True
                if group := manager.task_group:
                    group.stop = True
                    if group.blocking_task is not None:
                        group.blocking_task.cancel()
                await asyncio.wait_for(running, 15)
        finally:
            # Also handle partial setup failures before the manager entered its
            # cleanup phase. Dispose real plugin effects while their loop is alive.
            roots = [key for key in plugin_service.plugins if key not in plugin_service._subplugined]
            for plugin_id in reversed(roots):
                if plug := plugin_service.plugins.get(plugin_id):
                    if tasks := plug.dispose(is_cleanup=True):
                        await asyncio.gather(*tasks)
            if _scopes.get("$global") is root_scope:
                await root_scope.cleanup()


if __name__ == "__main__":
    sys.path.insert(0, sys.argv[1])
    asyncio.run(main())
