"""Exercise native WebUI configuration, reload ownership, and isolated chat transports."""

from __future__ import annotations

import os
import sys
import json
from pathlib import Path
import subprocess

import pytest

PROBE = """from arclet.entari import BasicConfModel, Plugin, command, metadata

class Config(BasicConfModel):
    message: str = "stable"

metadata(name="Native WebUI probe", config=Config)
Plugin.current().register_schema("properties.message", {"title": "Localized message"})

@command.on("webui_probe")
def respond():
    return "stable"
"""

FAMILY = """from arclet.entari import package
package("family.child")
from . import child
"""

CHILD = """from arclet.entari import Plugin
Plugin.current().register_schema((".child", "properties", "child_value"), {"type": "integer", "title": "Child value"})
"""

NATIVE_RUNNER = r"""
async def main():
    from arclet.entari import AiohttpClientService
    from arclet.entari.plugin.service import plugin_service
    from arclet.letoderea.scope import Scope, _scopes

    set_event_loop(asyncio.get_running_loop())
    root_scope = Scope.root()
    manager = it(Launart)
    running = None
    try:
        app = Entari.load("entari.json")
        manager.add_component(AiohttpClientService())
        # This invokes Entari.ensure_manager once, loading all configured
        # plugins and their real services in the native order.
        manager.add_component(app)
        running = asyncio.create_task(manager.launch())
        await asyncio.wait_for(plugin_service.status.wait_for("blocking"), 15)
        await scenario(app)
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
            # Dispose partial initialization too, before asyncio closes the
            # loop used by Entari's collected plugin effects/finalizers.
            roots = [key for key in plugin_service.plugins if key not in plugin_service._subplugined]
            for plugin_id in reversed(roots):
                if plug := plugin_service.plugins.get(plugin_id):
                    if tasks := plug.dispose(is_cleanup=True):
                        await asyncio.gather(*tasks)
            if _scopes.get("$global") is root_scope:
                await root_scope.cleanup()

asyncio.run(main())
"""

SCENARIO = r"""import asyncio
import json
from pathlib import Path

from arclet.entari import Entari, command
from creart import it
from launart import Launart
from arclet.entari.plugin import find_plugin, load_plugin
from arclet.letoderea.utils import set_event_loop

async def scenario(app):
    from entari_plugin_webui.services.config_service import (
        get_etag, get_schema_for_section, get_section, list_sections, update_section,
    )
    from entari_plugin_webui.services.plugin_service import list_plugins, reload_plugin, toggle_plugin
    from entari_plugin_webui.core.error import ConfigConflict, ConfigPreconditionRequired, PluginError
    result = load_plugin("probe", {})
    assert result, result

    rows = {item["id"]: item for item in list_plugins()}
    assert "$" not in rows
    assert {"$.commands", "$.localdata", "$.record_message", "$.scheduler"} <= rows.keys()
    assert rows["$.commands"]["type"] == "rootless"
    sections = list_sections()["plugin_sections"]
    assert sections["plugins:.localdata"] == "$.localdata"
    assert "plugins:$" not in sections
    assert get_section("plugins:$.localdata")["app_name"] == "native-webui-probe"
    assert "app_name" in get_schema_for_section("plugins:$.localdata")["schema"]["properties"]
    schema = get_schema_for_section("plugins:probe")["schema"]
    assert schema["properties"]["message"]["title"] == "Localized message"
    assert rows["family"]["has_metadata"] is False
    assert rows["family.child"]["parent"] == "family"
    assert rows["family.child"]["has_config_schema"] is True
    assert get_section("plugins:family.child") == {"child_value": 2, "remove_me": 3}
    child_schema = get_schema_for_section("plugins:family.child")["schema"]["properties"]
    assert child_schema["child_value"]["title"] == "Child value"
    assert "parent_value" not in child_schema

    siblings = {key: find_plugin(key).is_available for key in find_plugin("$").subplugins}
    try:
        await toggle_plugin("$.record_message", enable=False)
    except PluginError:
        pass
    else:
        raise AssertionError("Rootless controls must respect the native manager's protected boundary")
    assert {key: find_plugin(key).is_available for key in siblings} == siblings
    assert await command.execute("webui_probe") == "stable"

    before = Path("entari.json").read_bytes()
    for if_match, error in ((None, ConfigPreconditionRequired), ("outdated", ConfigConflict)):
        try:
            await update_section("plugins:$.localdata", {"app_name": "not-saved"}, if_match=if_match)
        except error:
            pass
        else:
            raise AssertionError("Missing or stale If-Match was accepted")
        assert Path("entari.json").read_bytes() == before
    report = await update_section("plugins:$.localdata", {"app_name": "updated-native-name"}, if_match=get_etag())
    assert report.ok, report
    persisted = json.loads(Path("entari.json").read_text(encoding="utf-8"))["plugins"]
    assert persisted[".localdata"]["app_name"] == "updated-native-name"
    assert "$.localdata" not in persisted
    assert "$" not in persisted
    report = await update_section("plugins:family.child", {"child_value": 4}, if_match=get_etag())
    assert report.ok, report
    persisted = json.loads(Path("entari.json").read_text(encoding="utf-8"))["plugins"]
    assert persisted["family"]["parent_value"] == 1
    assert persisted["family"][".child"] == {"child_value": 4}
    assert "family.child" not in persisted
    assert get_section("plugins:family.child") == {"child_value": 4}

    original = Path("probe.py").read_text(encoding="utf-8")
    Path("probe.py").write_text(original + '\nraise RuntimeError("intentional failure")\n', encoding="utf-8")
    assert await reload_plugin("probe") is False
    assert await command.execute("webui_probe") == "stable"
    assert get_schema_for_section("plugins:probe")["schema"]["properties"]["message"]["title"] == "Localized message"
    Path("probe.py").write_text(original.replace('return "stable"', 'return "replaced"'), encoding="utf-8")
    assert await reload_plugin("probe") is True
    assert await command.execute("webui_probe") == "replaced"

"""

SOCKET_SCENARIO = r"""import asyncio
import json

from arclet.entari import Entari
from creart import it
from launart import Launart
from arclet.letoderea.utils import set_event_loop
from satori import EventType, LoginStatus

class Socket:
    def __init__(self, cookies):
        self.cookies = cookies
        self.accepted = asyncio.Event()
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.closed = None

    async def accept(self):
        self.accepted.set()

    async def close(self, **kwargs):
        self.closed = kwargs

    async def receive(self):
        return await self.incoming.get()

    async def send_text(self, payload):
        await self.outgoing.put(json.loads(payload))

class EventSink:
    def __init__(self):
        self.events = []
        self.fail_login = False

    async def post(self, event):
        self.events.append(event)
        if self.fail_login and event.type == EventType.LOGIN_ADDED:
            self.fail_login = False
            raise RuntimeError("intentional login publication failure")

async def scenario(app):
    from entari_plugin_webui import _session_store
    from entari_plugin_webui.adapter import WebUIAdapter
    from entari_plugin_webui.core.security import set_local_mode

    cookie = _session_store.create(ip="isolated transport probe")
    for local in (False, True):
        set_local_mode(local)
        adapter = WebUIAdapter()
        sink = EventSink()
        adapter.ensure_server(sink)
        cookies = {} if local else {"webui_sid": cookie}
        first, second = Socket(cookies), Socket(cookies)
        first_task = asyncio.create_task(adapter.websocket_server_handler(first))
        second_task = asyncio.create_task(adapter.websocket_server_handler(second))
        await asyncio.wait_for(asyncio.gather(first.accepted.wait(), second.accepted.wait()), 3)
        assert len(adapter.connections) == len(adapter.logins) == 2
        connections = list(adapter.connections.values())
        first_conn = next(conn for conn in connections if conn.connection is first)
        second_conn = next(conn for conn in connections if conn.connection is second)
        assert first_conn.session_id != second_conn.session_id
        assert first_conn.session_id != cookie != second_conn.session_id
        assert first_conn.user_id != second_conn.user_id

        if not local:
            invalid = Socket({"webui_sid": "invalid"})
            await adapter.websocket_server_handler(invalid)
            assert invalid.closed["code"] == 1008
            assert not invalid.accepted.is_set()
            assert len(adapter.connections) == 2

        first_call = asyncio.create_task(first_conn.call_api("message_create"))
        second_call = asyncio.create_task(second_conn.call_api("message_create"))
        await asyncio.wait_for(first.outgoing.get(), 3)
        second_payload = await asyncio.wait_for(second.outgoing.get(), 3)
        if local:
            first_task.cancel()
            try:
                await first_task
            except asyncio.CancelledError:
                pass
        else:
            await first.incoming.put({"type": "websocket.disconnect"})
            await asyncio.wait_for(first_task, 3)
        try:
            await first_call
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("Closing a transport did not cancel its pending response")
        assert first_conn.session_id not in adapter.connections
        assert first_conn.session_id not in adapter.logins
        assert adapter.connections[second_conn.session_id] is second_conn
        assert adapter.logins[second_conn.session_id].status == LoginStatus.ONLINE
        assert not second_call.done()
        await second.incoming.put({"type": "websocket.receive", "text": json.dumps({
            "token": second_payload["token"], "status": "ok", "data": {"id": "second-only"},
        })})
        assert await asyncio.wait_for(second_call, 3) == {"id": "second-only"}
        await second.incoming.put({"type": "websocket.disconnect"})
        await asyncio.wait_for(second_task, 3)
        assert not adapter.connections and not adapter.logins
        removed = [event for event in sink.events if event.type == EventType.LOGIN_REMOVED]
        assert len(removed) == 2
        assert {event.login.user.id for event in removed} == {first_conn.session_id, second_conn.session_id}

        sink.fail_login = True
        failed = Socket(cookies)
        try:
            await adapter.websocket_server_handler(failed)
        except RuntimeError as error:
            assert "intentional login publication failure" in str(error)
        else:
            raise AssertionError("Expected publication failure")
        assert not adapter.connections and not adapter.logins

"""

EXTENSION_SCENARIO = r"""import asyncio

import httpx
from creart import it
from launart import Launart
from arclet.entari import Entari
from arclet.entari.plugin import load_plugin
from arclet.entari.plugin.loader import reload_plugin
from arclet.letoderea.utils import set_event_loop

async def scenario(app):
    from entari_plugin_server import get_asgi
    from entari_plugin_webui.core.extension import get_all_menus, get_all_pages, webui_extend
    from entari_plugin_webui.api.deps import get_session_store
    from entari_plugin_webui.core.security import set_local_mode

    set_local_mode(False)
    sid = get_session_store().create(ip="127.0.0.1")

    independent = webui_extend("independent-ownership-probe")
    independent.add_menu("independent", "mdi:puzzle", "/extension/independent")
    independent.add_page("independent", "independent", "mdi:puzzle", "/api/independent/page")
    pages = {
        "meme_webui": ("llm-chat-memes", "/api/llm-chat/memes/page"),
        "agent_webui": ("llm-chat-sessions", "/api/llm-chat/sessions/page"),
        "reminder_webui": ("llm-chat-reminders", "/api/llm-chat/reminders/page"),
    }
    loaded = {}
    for name in pages:
        result = load_plugin(f"plugins.llm_chat.{name}", {})
        assert result, result
        loaded[name] = result.plugin

    def assert_entries():
        menus = [row["path"] for row in get_all_menus()]
        definitions = [row["key"] for row in get_all_pages()]
        for key, _ in pages.values():
            assert menus.count(f"/extension/{key}") == 1, (key, menus)
            assert definitions.count(key) == 1, (key, definitions)
        assert menus.count("/extension/independent") == 1
        assert definitions.count("independent") == 1

    transport = httpx.ASGITransport(app=get_asgi())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://localhost", headers={"Cookie": f"webui_sid={sid}"}
    ) as client:
        assert_entries()
        for _ in range(2):
            for name in pages:
                result = await reload_plugin(loaded[name].id)
                assert result, result
                loaded[name] = result.plugin
                assert_entries()
                for _, path in pages.values():
                    response = await client.get(path)
                    assert response.status_code == 200, (path, response.status_code)
                    assert response.headers["Content-Type"].startswith("text/html")
        for name, plug in loaded.items():
            if tasks := plug.dispose():
                await asyncio.gather(*tasks)
            key, path = pages[name]
            assert f"/extension/{key}" not in {row["path"] for row in get_all_menus()}
            assert key not in {row["key"] for row in get_all_pages()}
            assert (await client.get(path)).status_code == 404
        assert [row["path"] for row in get_all_menus()] == ["/extension/independent"]
        assert [row["key"] for row in get_all_pages()] == ["independent"]
"""


@pytest.mark.parametrize(
    "scenario",
    [SCENARIO + NATIVE_RUNNER, SOCKET_SCENARIO + NATIVE_RUNNER, EXTENSION_SCENARIO + NATIVE_RUNNER],
    ids=["native-config", "isolated-sockets", "extension-ownership"],
)
def test_native_webui_preserves_root_configuration_and_atomic_reload(tmp_path: Path, scenario: str) -> None:
    config = {
        "basic": {
            "network": [],
            "external_dirs": [str(tmp_path), str(Path(__file__).resolve().parents[1])],
            "log": {"level": "ERROR", "rich_error": False, "save": False},
            "schema": False,
        },
        "plugins": {
            "$prefix": [{"key": "", "plugins": ["probe", "family"]}],
            ".localdata": {"app_name": "native-webui-probe"},
            "database": {"name": str(tmp_path / "database.sqlite3")},
            "server": {"host": "127.0.0.1", "port": 0, "path": "satori", "adapters": []},
            "entari_plugin_webui": {},
            "probe": {},
            "family": {"parent_value": 1, ".child": {"child_value": 2, "remove_me": 3}},
        },
    }
    (tmp_path / "entari.json").write_text(json.dumps(config), encoding="utf-8")
    (tmp_path / "probe.py").write_text(PROBE, encoding="utf-8")
    (tmp_path / "family").mkdir()
    (tmp_path / "family" / "__init__.py").write_text(FAMILY, encoding="utf-8")
    (tmp_path / "family" / "child.py").write_text(CHILD, encoding="utf-8")
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP"}
    }
    environment.update({key: str(tmp_path) for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA")})
    completed = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", scenario],
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
