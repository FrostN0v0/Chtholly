"""Exercise native root-niche configuration and atomic WebUI reload in isolation."""

from __future__ import annotations

import os
import sys
import json
from pathlib import Path
import subprocess

PROBE = """from arclet.entari import BasicConfModel, Plugin, command, metadata

class Config(BasicConfModel):
    message: str = "stable"

metadata(name="Native WebUI probe", config=Config)
Plugin.current().register_schema("properties.message", {"title": "Localized message"})

@command.on("webui_probe")
def respond():
    return "stable"
"""

SCENARIO = r"""import asyncio
import json
from pathlib import Path

from arclet.entari import Entari, command
from arclet.entari.plugin import find_plugin, load_plugin
from arclet.letoderea.utils import set_event_loop

async def main():
    set_event_loop(asyncio.get_running_loop())
    app = Entari.load("entari.json")
    # Core niches publish their metadata when the native manager enters blocking.
    for plugin_id in find_plugin("$").subplugins:
        find_plugin(plugin_id).exec_apply()
    from entari_plugin_webui.services.config_service import (
        get_schema_for_section, get_section, list_sections, update_section,
    )
    from entari_plugin_webui.services.plugin_service import list_plugins, reload_plugin, toggle_plugin
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

    siblings = {key: find_plugin(key).is_available for key in ("$.commands", "$.localdata", "$.scheduler")}
    assert await toggle_plugin("$.record_message", enable=False)
    assert not find_plugin("$.record_message").is_available
    assert {key: find_plugin(key).is_available for key in siblings} == siblings
    assert await toggle_plugin("$.record_message", enable=True)
    assert find_plugin("$.record_message").is_available
    assert await command.execute("webui_probe") == "stable"

    update_section("plugins:$.localdata", {"app_name": "updated-native-name"})
    persisted = json.loads(Path("entari.json").read_text(encoding="utf-8"))["plugins"]
    assert persisted[".localdata"]["app_name"] == "updated-native-name"
    assert "$.localdata" not in persisted
    assert "$" not in persisted

    original = Path("probe.py").read_text(encoding="utf-8")
    Path("probe.py").write_text(original + '\nraise RuntimeError("intentional failure")\n', encoding="utf-8")
    assert await reload_plugin("probe") is False
    assert await command.execute("webui_probe") == "stable"
    assert get_schema_for_section("plugins:probe")["schema"]["properties"]["message"]["title"] == "Localized message"
    Path("probe.py").write_text(original.replace('return "stable"', 'return "replaced"'), encoding="utf-8")
    assert await reload_plugin("probe") is True
    assert await command.execute("webui_probe") == "replaced"

asyncio.run(main())
"""


def test_native_webui_preserves_root_configuration_and_atomic_reload(tmp_path: Path) -> None:
    config = {
        "basic": {
            "network": [],
            "external_dirs": [str(tmp_path)],
            "log": {"level": "ERROR", "rich_error": False, "save": False},
            "schema": False,
        },
        "plugins": {
            "$prefix": [{"key": "", "plugins": ["probe"]}],
            ".localdata": {"app_name": "native-webui-probe"},
            "database": {"name": str(tmp_path / "database.sqlite3")},
            "server": {"host": "127.0.0.1", "port": 0, "path": "satori", "adapters": []},
            "entari_plugin_webui": {},
            "probe": {},
        },
    }
    (tmp_path / "entari.json").write_text(json.dumps(config), encoding="utf-8")
    (tmp_path / "probe.py").write_text(PROBE, encoding="utf-8")
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP"}
    }
    environment.update({key: str(tmp_path) for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA")})
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
