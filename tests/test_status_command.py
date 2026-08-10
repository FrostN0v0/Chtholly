"""Status command registration must use an Alconna instance and exact shortcuts."""

import sys
from pathlib import Path
import subprocess


def test_status_command_uses_alconna_with_exact_shortcuts(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    config_path = tmp_path / "entari.yml"
    config_path.write_text(
        f'basic:\n  external_dirs: ["{(root / "plugins").as_posix()}"]\nplugins: {{}}\n',
        encoding="utf-8",
    )
    script = f"""
import sys
from pathlib import Path

root = Path({str(root)!r})
sys.path.insert(0, str(root))
sys.path.insert(0, str(root / "plugins"))

from arclet.alconna import Alconna, command_manager
from arclet.entari.config import EntariConfig
from arclet.entari.plugin import load_plugin

EntariConfig.load(Path({str(config_path)!r}))
plugin = load_plugin("status", config={{}})
assert plugin is not None
module = plugin.module
assert isinstance(module.status_alconna, Alconna)
command = next(item for item in command_manager.get_commands() if item.command == "status")
assert command is module.status_alconna
assert set(command.get_shortcuts()) == {{"botstatus", "\u72b6\u6001", "\u8fd0\u884c\u72b6\u6001"}}
for trigger in ("status", "botstatus", "\u72b6\u6001", "\u8fd0\u884c\u72b6\u6001"):
    assert command.parse(trigger).matched, trigger
assert not command.parse("\u72b6\u6001 extra").matched
print("ok")
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr

    assert completed.stdout.strip().endswith("ok")
