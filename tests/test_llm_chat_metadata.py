"""llm_chat metadata must expose its configuration schema."""

from __future__ import annotations

import sys
from pathlib import Path
import subprocess


def test_llm_chat_metadata_exposes_config_schema(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    config_path = tmp_path / "entari.yml"
    config_path.write_text(
        f'basic:\n  external_dirs: ["{(root / "plugins").as_posix()}"]\nplugins: {{}}\n',
        encoding="utf-8",
    )
    script = f"""
import sys
from pathlib import Path
from types import ModuleType

root = Path({str(root)!r})
sys.path.insert(0, str(root))
sys.path.insert(0, str(root / "plugins"))

litellm = ModuleType("litellm")
litellm.suppress_debug_info = False
sys.modules["litellm"] = litellm

agno_compat = ModuleType("llm_chat.agno_compat")
agno_compat.install_agno_tool_bridge = lambda: None
sys.modules[agno_compat.__name__] = agno_compat
for child in ("chat_handler", "tag_runtime", "meme_command", "meme_webui"):
    sys.modules[f"llm_chat.{{child}}"] = ModuleType(f"llm_chat.{{child}}")
tool_runtime = ModuleType("llm_chat.tool_runtime")
tool_runtime.registered_tools = []
sys.modules[tool_runtime.__name__] = tool_runtime

from arclet.entari.config import EntariConfig
from arclet.entari.plugin import load_plugin

EntariConfig.load(Path({str(config_path)!r}))
plugin = load_plugin("llm_chat", config={{}})
assert plugin is not None
assert plugin.metadata is not None
assert plugin.metadata.config is plugin.module.LLMChatWebUIConfig
schema = plugin.metadata.get_config_schema()
assert schema["type"] == "object"
properties = schema["properties"]
assert {{"persona", "allowed_commands"}} <= properties.keys()
assert properties["persona"]["title"] == "人格设定"
assert properties["persona"]["description"].startswith("仅填写角色人格文本")
assert all(any("\u4e00" <= char <= "\u9fff" for char in item["title"]) for item in properties.values())
assert all(any("\u4e00" <= char <= "\u9fff" for char in item["description"]) for item in properties.values())
from llm_chat.config_schema import _CONFIG_SCHEMA_TEXT
assert {{
    "tool_context_max_events",
    "tool_context_max_chars",
    "tool_history_max_records_per_channel",
    "channel_message_max_images",
    "self_reference_image",
}} <= _CONFIG_SCHEMA_TEXT.keys()
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
