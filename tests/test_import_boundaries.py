"""Import-safe packages must not execute Entari plugin entrypoints."""

import sys
from types import ModuleType
from pathlib import Path
import importlib


def test_domain_and_tts_core_imports_do_not_load_plugin_entrypoints():
    plugins_package = importlib.import_module("plugins")
    previous_package = sys.modules.get("plugins.llm_chat")
    package = previous_package
    if package is None:
        package = ModuleType("plugins.llm_chat")
        package.__path__ = [str(Path(__file__).resolve().parents[1] / "plugins" / "llm_chat")]
        sys.modules["plugins.llm_chat"] = package
        setattr(plugins_package, "llm_chat", package)

    try:
        for name in [
            "plugins.llm_chat.core.media",
            "plugins.llm_chat.core.profile",
            "plugins.llm_chat.core.memory_policy",
            "plugins.llm_chat.core.eval",
            "plugins.llm_chat.core.compose",
            "plugins.llm_chat.core.forward",
            "plugins.llm_chat.core.types",
            "plugins.llm_chat.web_access",
            "utils.tts_service_core.providers.gpt_sovits",
            "utils.tts_service_core.providers.fish_audio",
            "utils.tts_service_core.providers.factory",
        ]:
            importlib.import_module(name)

        assert "plugins.llm_chat.chat_handler" not in sys.modules
        assert "plugins.llm_chat.tool_runtime" not in sys.modules
        assert "plugins.llm_chat.web_tools" not in sys.modules
        assert "plugins.llm_chat.generation" not in sys.modules
        assert "plugins.llm_chat.tag_runtime" not in sys.modules
        assert "plugins.tts_service" not in sys.modules
    finally:
        if previous_package is None:
            sys.modules.pop("plugins.llm_chat", None)
            if getattr(plugins_package, "llm_chat", None) is package:
                delattr(plugins_package, "llm_chat")
