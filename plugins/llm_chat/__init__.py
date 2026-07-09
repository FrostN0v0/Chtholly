"""Interactive group chat plugin runtime entrypoint."""
# ruff: noqa: I001

from arclet.entari import metadata
from arclet.entari.logger import log
from arclet.entari.plugin import PluginRole
from arclet.entari.plugin.model import Plugin

HELP_META = {"icon": "💬", "category": "互动"}

_LOGGER = log.wrapper("[llm_chat]")

plug = Plugin.current()

if plug is not None:
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

    _LOGGER.info(f"registered LLM tools: {', '.join(tool_runtime.registered_tools) or '(none)'}")
