"""Import-safe packages must not execute Entari plugin entrypoints."""

import sys
from pathlib import Path
import subprocess


def test_domain_and_tts_core_imports_do_not_load_plugin_entrypoints():
    root = Path(__file__).resolve().parents[1]
    modules = [
        "plugins.llm_chat.core.media",
        "plugins.llm_chat.core.profile",
        "plugins.llm_chat.core.memory_policy",
        "plugins.llm_chat.core.eval",
        "plugins.llm_chat.core.compose",
        "plugins.llm_chat.core.forward",
        "plugins.llm_chat.core.types",
        "plugins.llm_chat.core.image_source",
        "plugins.llm_chat.core.native_images",
        "plugins.llm_chat.core.media_delivery",
        "plugins.llm_chat.web",
        "plugins.llm_chat.web.policy",
        "plugins.llm_chat.web.exa",
        "plugins.llm_chat.tools",
        "plugins.llm_chat.tools.support",
        "plugins.llm_chat.tools.send_external_image",
        "plugins.llm_chat.tools.get_local_time",
        "plugins.llm_chat.turn_lifecycle",
        "utils.tts_service_core.providers.gpt_sovits",
        "utils.tts_service_core.providers.fish_audio",
        "utils.tts_service_core.providers.factory",
    ]
    forbidden = [
        "plugins.llm_chat.chat_handler",
        "plugins.llm_chat.tool_runtime",
        "plugins.llm_chat.meme_command",
        "plugins.llm_chat.meme_store",
        "plugins.llm_chat.generation",
        "plugins.llm_chat.tag_runtime",
        "plugins.tts_service",
    ]
    script = f"""
import importlib
import sys
from pathlib import Path
from types import ModuleType

import plugins

package = ModuleType("plugins.llm_chat")
package.__path__ = [str(Path.cwd() / "plugins" / "llm_chat")]
sys.modules["plugins.llm_chat"] = package
setattr(plugins, "llm_chat", package)
for name in {modules!r}:
    importlib.import_module(name)
for name in {forbidden!r}:
    assert name not in sys.modules, name
print("ok")
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout.strip() == "ok"


def test_web_policy_import_does_not_load_provider_adapter():
    root = Path(__file__).resolve().parents[1]
    script = """
import importlib
import sys
from pathlib import Path
from types import ModuleType

import plugins

package = ModuleType("plugins.llm_chat")
package.__path__ = [str(Path.cwd() / "plugins" / "llm_chat")]
sys.modules["plugins.llm_chat"] = package
setattr(plugins, "llm_chat", package)
importlib.import_module("plugins.llm_chat.web.policy")
assert "plugins.llm_chat.web.exa" not in sys.modules
assert "agno.tools.exa" not in sys.modules
print("ok")
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout.strip() == "ok"
