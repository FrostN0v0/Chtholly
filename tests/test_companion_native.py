"""Consumer-visible companion routing and shared-turn integration regression."""

from __future__ import annotations

import os
import sys
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_native_companion_routing_steering_and_receipts(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment.pop("PYTHONHOME", None)
    environment.update(PYTHONPATH=str(ROOT), LITELLM_LOCAL_MODEL_COST_MAP="True")
    result = subprocess.run(
        [sys.executable, str(ROOT / "tests" / "fixtures" / "companion_dialogue_probe.py")],
        cwd=tmp_path,
        env=environment,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
