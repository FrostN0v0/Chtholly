"""Validate native adapter configuration through both public schema surfaces."""

from __future__ import annotations

import os
import sys
import json
from pathlib import Path
import subprocess

import pytest

PROBE = Path(__file__).parent / "fixtures" / "server_schema_probe.py"


@pytest.mark.parametrize("server_key", ["server", "entari_plugin_server"])
def test_adapter_schemas_preserve_nested_validation(tmp_path: Path, server_key: str) -> None:
    configuration = {
        "basic": {"network": [], "log": {"level": "ERROR", "rich_error": False}, "schema": False},
        "plugins": {
            ".localdata": {"app_name": "adapter-schema-probe"},
            "database": {"name": str(tmp_path / "state.sqlite3")},
            server_key: {
                "host": "127.0.0.1",
                "port": 0,
                "direct_adapter": True,
                "adapters": [
                    {
                        "$path": "@qq.websocket",
                        "app_id": "isolated-app",
                        "secret": "not-a-real-credential",
                        "intent": {"c2c_group_at_messages": True},
                    },
                    {"$path": "@qq.main", "secrets": {"isolated-app": "not-a-real-credential"}},
                ],
            },
            "entari_plugin_webui": {},
        },
        "adapters": [{"$path": "@onebot11.reverse", "path": "schema-probe"}],
    }
    (tmp_path / "entari.json").write_text(json.dumps(configuration), encoding="utf-8")
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP"}
    }
    environment.update({key: str(tmp_path) for key in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA")})
    result = subprocess.run(
        [sys.executable, "-X", "utf8", str(PROBE), server_key],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
