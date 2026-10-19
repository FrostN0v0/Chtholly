"""Native Entari reply/receipt association regression."""

from __future__ import annotations

import os
import sys
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_native_quote_and_individual_receipt_association(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment.update(PYTHONPATH=str(ROOT), LITELLM_LOCAL_MODEL_COST_MAP="True")
    result = subprocess.run(
        [sys.executable, str(ROOT / "tests" / "fixtures" / "channel_reply_probe.py")],
        cwd=tmp_path,
        env=environment,
        text=True,
        encoding="utf-8",
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
