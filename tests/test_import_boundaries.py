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
        "plugins.llm_chat.core.agent_trace",
        "plugins.llm_chat.core.native_images",
        "plugins.llm_chat.core.media_delivery",
        "plugins.llm_chat.web",
        "plugins.llm_chat.web.policy",
        "plugins.llm_chat.web.exa",
        "plugins.llm_chat.tools",
        "plugins.llm_chat.tools.support",
        "plugins.llm_chat.tools._tts",
        "plugins.llm_chat.tools.list_tts_voices",
        "plugins.llm_chat.tools.synthesize_speech",
        "plugins.llm_chat.tools.prepare_external_media",
        "plugins.llm_chat.tools._rendering",
        "plugins.llm_chat.tools._render_policy",
        "plugins.llm_chat.tools._report",
        "plugins.llm_chat.tools.markdown2pic",
        "plugins.llm_chat.tools.html2pic",
        "plugins.llm_chat.tools.jinja2pic",
        "plugins.llm_chat.tools.get_local_time",
        "plugins.llm_chat.turn_lifecycle",
        "utils.reminder_core",
        "utils.tts_service_core.voice_catalog",
        "utils.tts_service_core.providers.gpt_sovits",
        "utils.tts_service_core.providers.gpt_sovits_catalog",
        "utils.tts_service_core.providers.fish_audio",
        "utils.tts_service_core.providers.factory",
    ]
    forbidden = [
        "entari_plugin_llm",
        "plugins.llm_chat.chat_handler",
        "plugins.llm_chat.tool_runtime",
        "plugins.llm_chat.meme_store",
        "plugins.llm_chat.generation",
        "plugins.llm_chat.tag_runtime",
        "plugins.llm_chat.agent_events",
        "plugins.llm_chat.session_manager",
        "plugins.tts_service",
    ]
    script = f"""
import importlib
import sys
for name in {modules!r}:
    importlib.import_module(name)
for name in {forbidden!r}:
    assert name not in sys.modules, name
"""

    subprocess.run(
        [sys.executable, "-E", "-X", "utf8", "-c", script],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_web_policy_import_does_not_load_provider_adapter():
    root = Path(__file__).resolve().parents[1]
    script = """
import importlib
import sys
importlib.import_module("plugins.llm_chat.web.policy")
assert "plugins.llm_chat.web.exa" not in sys.modules
assert "agno.tools.exa" not in sys.modules
"""

    subprocess.run(
        [sys.executable, "-E", "-X", "utf8", "-c", script],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
