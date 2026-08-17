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
        "plugins.llm_chat.tools._tts",
        "plugins.llm_chat.tools.list_tts_voices",
        "plugins.llm_chat.tools.speak",
        "plugins.llm_chat.tools.send_external_image",
        "plugins.llm_chat.tools.get_local_time",
        "plugins.llm_chat.turn_lifecycle",
        "utils.tts_service_core.voice_catalog",
        "utils.tts_service_core.providers.gpt_sovits",
        "utils.tts_service_core.providers.gpt_sovits_catalog",
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


def test_llm_chat_entrypoint_runs_under_entari_module_proxy():
    root = Path(__file__).resolve().parents[1]
    script = """
import sys
from pathlib import Path
from types import ModuleType
from weakref import proxy
from importlib.util import module_from_spec, spec_from_file_location

import arclet.entari as entari
import arclet.entari.plugin as entari_plugin
from arclet.entari.plugin.model import Plugin, current_plugin

root = Path.cwd()
package_name = "_llm_chat_entrypoint_test"
package_dir = root / "plugins" / "llm_chat"
spec = spec_from_file_location(
    package_name,
    package_dir / "__init__.py",
    submodule_search_locations=[str(package_dir)],
)
assert spec is not None
assert spec.loader is not None
module = module_from_spec(spec)
plugin = Plugin(package_name, module, config={})
setattr(module, "__plugin__", plugin)
sys.modules[package_name] = proxy(module)

litellm = ModuleType("litellm")
litellm.suppress_debug_info = False
sys.modules["litellm"] = litellm
sys.modules["channel_perception"] = ModuleType("channel_perception")

bridge_calls = []
agno_compat = ModuleType(f"{package_name}.agno_compat")
agno_compat.install_agno_tool_bridge = lambda: bridge_calls.append("installed")
tool_runtime = ModuleType(f"{package_name}.tool_runtime")
tool_runtime.registered_tools = ["sentinel_tool"]
submodules = {
    "agno_compat": agno_compat,
    "chat_handler": ModuleType(f"{package_name}.chat_handler"),
    "tag_runtime": ModuleType(f"{package_name}.tag_runtime"),
    "tool_runtime": tool_runtime,
    "meme_command": ModuleType(f"{package_name}.meme_command"),
}
for suffix, submodule in submodules.items():
    sys.modules[f"{package_name}.{suffix}"] = submodule
    setattr(module, suffix, submodule)

entari.metadata = lambda *args, **kwargs: None
entari_plugin.collect_disposes = lambda callback: None
token = current_plugin.set(plugin)
try:
    spec.loader.exec_module(module)
finally:
    current_plugin.reset(token)

assert bridge_calls == ["installed"]
assert module.registered_tools == ["sentinel_tool"]
print("ok")
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout.strip().endswith("ok")


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
