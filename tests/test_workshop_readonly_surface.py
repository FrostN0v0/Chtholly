"""Exercise authenticated workshop reads and native ownership without running candidates."""

from __future__ import annotations

import os
import sys
import json
from pathlib import Path
import subprocess

SCENARIO = r"""import asyncio
from datetime import datetime, timezone

import httpx
from satori import EventType, ChannelType
from satori.model import User, Event, Login, Channel, MessageObject
from satori.client.account import Account, ApiInfo
from arclet.entari import Entari, command
from arclet.entari.event.base import MessageCreatedEvent
from arclet.entari.plugin import find_plugin, load_plugin, unload_plugin_async
from arclet.entari.session import Session, EntariProtocol
from arclet.letoderea.utils import set_event_loop
from starlette.requests import Request

from utils.plugin_workshop_core.models import Actor, CheckResult, ValidationReport
from utils.plugin_workshop_core.policy import parse_manifest


async def main():
    set_event_loop(asyncio.get_running_loop())
    Entari.load("entari.json")
    for plugin_id in find_plugin("$").subplugins:
        find_plugin(plugin_id).exec_apply()

    import entari_plugin_webui as webui
    from entari_plugin_server import get_asgi
    from entari_plugin_webui.core.security import is_local_mode, set_local_mode, verify_password

    app = get_asgi()
    baseline_subscribers = set(command._commands.subscribers)
    assert load_plugin("plugin_workshop", {})
    workshop_plugin = find_plugin("plugin_workshop")
    service = workshop_plugin.module.workshop
    assert not is_local_mode()
    assert verify_password("workshop-read-test-password", webui.webui_config.password)
    from plugin_workshop.webui_auth import register_password_authentication
    passwordless_sid = webui._session_store.create(ip="old passwordless session")
    set_local_mode(True)
    try:
        register_password_authentication()
    except RuntimeError as error:
        assert "passwordless" in str(error)
    else:
        raise AssertionError("Existing passwordless sessions were promoted")
    finally:
        webui._session_store.destroy(passwordless_sid)
        set_local_mode(False)

    class ReadSandbox:
        async def status(self):
            return {"available": False, "reason": "Read-only regression does not run acceptance"}

        async def validate(self, request):
            raise AssertionError("A read-only view invoked acceptance")

        async def aclose(self):
            pass

    await service.sandbox.aclose()
    service.sandbox = ReadSandbox()
    await service.prepare()
    await service.restore()
    assert (await service.get_status())["ready"]
    source = ("raise AssertionError('candidate source must never execute')\n"
              "# <script>globalThis.executed = true</script>\n")
    manifest = parse_manifest({
        "title": "Read-only probe", "description": "<img src=x onerror=alert(1)>",
        "commands": ["workshop_read_probe"], "permissions": [],
        "data_description": "No operations in this regression", "configuration": {},
        "checks": [{"command": "workshop_read_probe", "expected_contains": "probe"}],
    })
    submitter = Actor("host-superuser", scope_id=7, is_admin=True, is_superuser=True)
    record = service.store.create_version("readonly_probe", {"__init__.py": source}, manifest, submitter)
    service.store.mark_validating(record.plugin_name, record.version)
    service.store.record_report(record.plugin_name, record.version, ValidationReport(
        "failed", record.source_hash, (CheckResult("fixture", False, "Stored read fixture; not executed"),),
        log="<script>globalThis.logExecuted = true</script>",
    ))

    writes = []
    async def reject_write(*args, **kwargs):
        writes.append((args, kwargs))
        raise AssertionError("Removed management surface invoked the backend")

    for method in ("submit", "validate", "approve", "activate", "rollback", "disable"):
        setattr(service, method, reject_write)

    root = "/api/plugin-workshop"
    version = root + "/projects/readonly_probe/versions/1"
    reads = [root + "/page", root + "/assets/app.js", root + "/assets/app.css", root + "/status",
             root + "/projects", root + "/projects/readonly_probe/versions", version, version + "/source"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        # Headers and unrelated cookies never substitute for a native password session.
        client.headers.update({"X-Superuser": "true", "X-Forwarded-User": "admin"})
        client.cookies.set("is_superuser", "true")
        for path in reads:
            response = await client.get(path)
            assert response.status_code == 401, (path, response.text)
            assert source not in response.text
        sid = webui._session_store.create(ip="authenticated test")
        client.cookies.set("webui_sid", sid)
        set_local_mode(True)
        assert (await client.get(version + "/source")).status_code == 401
        set_local_mode(False)

        from plugin_workshop.webui_api import authenticated_webui_actor
        actor = authenticated_webui_actor(Request({
            "type": "http", "headers": [(b"cookie", ("webui_sid=" + sid).encode()),
                                        (b"x-superuser", b"true")],
        }))
        assert actor.is_admin is True and actor.is_superuser is False

        nonces = []
        for _ in range(2):
            page = await client.get(root + "/page")
            assert page.status_code == 200
            assert page.headers["cache-control"] == "private, no-store"
            from html.parser import HTMLParser
            class Assets(HTMLParser):
                def __init__(self):
                    super().__init__()
                    self.assets = []
                    self.write_controls = []
                def handle_starttag(self, tag, attrs):
                    attributes = dict(attrs)
                    if tag in ("script", "style"):
                        self.assets.append((tag, attributes))
                    if tag in ("form", "dialog", "textarea", "input"):
                        self.write_controls.append(tag)
            parsed = Assets()
            parsed.feed(page.text)
            assert not parsed.write_controls
            policy = {part.split()[0]: part.split()[1:]
                      for part in page.headers["content-security-policy"].split(";") if part.strip()}
            assert policy["default-src"] == ["'none'"]
            assert policy["frame-ancestors"] == ["'self'"]
            assert policy["form-action"] == ["'none'"]
            assert {tag for tag, _ in parsed.assets} == {"script", "style"}
            assert len(parsed.assets) == 2
            nonce = parsed.assets[0][1]["nonce"]
            for tag, attributes in parsed.assets:
                assert attributes["nonce"] == nonce and "src" not in attributes
                assert policy[tag + "-src"] == ["'nonce-" + nonce + "'"]
            nonces.append(nonce)
        assert nonces[0] != nonces[1]

        for path in reads[1:]:
            response = await client.get(path)
            assert response.status_code == 200, (path, response.text)
            assert response.headers["x-content-type-options"] == "nosniff"
        assert (await client.get(version + "/source")).json()["item"] == {"__init__.py": source}
        detail = (await client.get(version)).json()["item"]
        history = (await client.get(root + "/projects/readonly_probe/versions")).json()["items"]
        assert history == [detail]
        assert detail["report"]["log"] == "<script>globalThis.logExecuted = true</script>"
        assert "approved_by" not in detail and "approved_at" not in detail
        for path in (root + "/projects?limit=101", root + "/projects?offset=1000001",
                     root + "/projects/readonly_probe/versions?limit=21",
                     root + "/projects/readonly_probe/versions?offset=257"):
            assert (await client.get(path)).status_code == 400
        assert (await client.get(root + "/projects?limit=1&offset=1")).json()["items"] == []
        assert (await client.get(root + "/projects/readonly_probe/versions?limit=1&offset=1")).json()["items"] == []

        mutations = [root + "/projects/readonly_probe/versions", root + "/projects/readonly_probe/disable"]
        mutations += [version + "/" + action for action in ("validate", "approve", "activate", "rollback")]
        for path in mutations:
            response = await client.post(path, headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
                                         json={"source_hash": record.source_hash, "acknowledge_native": True,
                                               "is_superuser": True})
            assert response.status_code in (404, 405), (path, response.status_code, response.text)
        for method in ("PUT", "PATCH", "DELETE"):
            response = await client.request(method, version, headers={"Origin": "http://testserver"},
                                            json={"is_superuser": True})
            assert response.status_code in (404, 405)
        webui._session_store.get(sid).expires_at = 0
        assert (await client.get(version + "/source")).status_code == 401

    account = Account(Login(sn=0, platform="probe", user=User("bot")), ApiInfo(), [], EntariProtocol)
    def session(user):
        event = Event(EventType.MESSAGE_CREATED, datetime.now(timezone.utc), account.self_info,
                      channel=Channel("channel", ChannelType.TEXT), user=User(user),
                      message=MessageObject("message", "workshop help"))
        return Session(account, MessageCreatedEvent(account, event))
    try:
        operator, ordinary = session("operator"), session("ordinary")
        for text in ("workshop help", "workshop list", "workshop show readonly_probe 1"):
            denied = str(await command.execute(text, ordinary))
            assert "权限不足" in denied and "readonly_probe" not in denied
        listed = str(await command.execute("workshop list", operator))
        shown = str(await command.execute("workshop show readonly_probe 1", operator))
        assert "readonly_probe" in listed and record.source_hash in shown
        assert "candidate source" not in shown and "logExecuted" not in shown
        for text in ("workshop approve readonly_probe 1", "workshop activate readonly_probe 1",
                     "workshop rollback readonly_probe 1", "workshop disable readonly_probe"):
            # Unknown subcommands may return native parser diagnostics rather than None.
            await command.execute(text, operator)
        assert not writes
        assert await unload_plugin_async("plugin_workshop")
        assert set(command._commands.subscribers) == baseline_subscribers
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            assert (await client.get(root + "/page")).status_code == 404
            assert (await client.get(version + "/source")).status_code == 404
            assert (await client.get("/api/health")).status_code == 200
        assert await command.execute("workshop list", operator) is None
        assert service._closed
    finally:
        await unload_plugin_async("plugin_workshop")
        await account.protocol.session.close()

asyncio.run(main())
"""


def test_native_workshop_surface_is_authenticated_readonly_and_owned(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    config = {
        "basic": {
            "network": [],
            "external_dirs": [str(root / "plugins")],
            "superusers": {"probe": ["operator"]},
            "log": {"level": "ERROR", "rich_error": False, "save": False},
            "schema": False,
        },
        "plugins": {
            "$prefix": [{"key": "", "plugins": ["plugin_workshop"]}],
            ".localdata": {"app_name": "workshop-readonly-regression"},
            "database": {"name": str(tmp_path / "database.sqlite3")},
            "server": {"host": "127.0.0.1", "port": 0, "path": "satori", "adapters": []},
            "entari_plugin_webui": {"password": "workshop-read-test-password"},
        },
    }
    (tmp_path / "entari.json").write_text(json.dumps(config), encoding="utf-8")
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP"}
    }
    environment.update({key: str(tmp_path) for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA")})
    environment["PYTHONPATH"] = str(root)
    completed = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", SCENARIO],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
