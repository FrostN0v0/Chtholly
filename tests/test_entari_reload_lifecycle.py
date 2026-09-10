"""Public Entari lifecycle regressions; each case owns a clean interpreter and cwd."""

import os
import sys
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize(
    "scenario",
    ["single", "repeated", "catalogue", "package", "subplugin", "replacement", "service"],
)
def test_entari_reload_lifecycle(scenario: str, tmp_path: Path) -> None:
    probe = Path(__file__).parent / "fixtures" / "entari_reload_probe.py"
    interpreter = os.environ.get("ENTARI_RELOAD_PYTHON", sys.executable)
    # Do not pass application credentials, dotenv selectors, or PYTHONPATH into probes.
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP"}
    }
    completed = subprocess.run(
        [interpreter, "-X", "utf8", str(probe.resolve()), scenario],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
    )
    assert completed.returncode == 0, (
        f"Entari lifecycle scenario {scenario!r} failed:\n{completed.stdout}\n{completed.stderr}"
    )
    assert f"PASS {scenario}" in completed.stdout.splitlines()
