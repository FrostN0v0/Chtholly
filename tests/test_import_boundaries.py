"""Import-safe packages must not execute Entari plugin entrypoints."""

import sys
import importlib


def test_domain_and_tts_core_imports_do_not_load_plugin_entrypoints():
    for name in [
        "utils.llm_chat_core.media",
        "utils.llm_chat_core.profile",
        "utils.llm_chat_core.eval",
        "utils.llm_chat_core.compose",
        "utils.tts_service_core.providers.gpt_sovits",
        "utils.tts_service_core.providers.fish_audio",
        "utils.tts_service_core.providers.factory",
    ]:
        importlib.import_module(name)

    assert "plugins.llm_chat" not in sys.modules
    assert "plugins.tts_service" not in sys.modules
