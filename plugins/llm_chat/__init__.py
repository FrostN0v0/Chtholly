"""Interactive group chat plugin runtime entrypoint."""
# ruff: noqa: I001

import litellm
from arclet.entari import metadata
from arclet.entari import plugin
from arclet.entari.logger import log
from arclet.entari.plugin import PluginRole
from arclet.entari.plugin.model import Plugin

HELP_META = {"icon": "💬", "category": "互动"}

_LOGGER = log.wrapper("[llm_chat]")

plug = Plugin.current()


def _configure_litellm_logging() -> None:
    previous = litellm.suppress_debug_info
    litellm.suppress_debug_info = True

    def restore() -> None:
        if litellm.suppress_debug_info is True:
            litellm.suppress_debug_info = previous

    plugin.collect_disposes(restore)


if plug is not None:
    _configure_litellm_logging()
    from .agno_compat import install_agno_tool_bridge  # entari: package

    install_agno_tool_bridge()

    metadata(
        name="llm_chat",
        author=[{"name": "FrostN0v0"}],
        version="0.1.0",
        description="群聊会话互动：多轴关系引擎 + 媒体互动 + 可选语音回复",
        role=PluginRole.NORMAL,
    )
    from . import chat_handler as chat_handler  # entari: package
    from . import tag_runtime as tag_runtime  # entari: package
    from . import tool_runtime as tool_runtime  # entari: package
    from . import meme_command as meme_command  # entari: package

    registered_tools = tool_runtime.registered_tools
    _LOGGER.info(f"registered LLM tools: {', '.join(registered_tools) or '(none)'}")
