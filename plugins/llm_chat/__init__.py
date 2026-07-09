"""Interactive group chat plugin runtime entrypoint."""

from arclet.entari import metadata, plugin_config
from arclet.entari.logger import log
from arclet.entari.plugin import PluginRole
from arclet.entari.plugin.model import Plugin

from .config import LLMChatConfig
from .tag_runtime import register_tag_runtime  # entari: package
from .chat_handler import register_chat_handler  # entari: package
from .tool_runtime import register_llm_tools  # entari: package

metadata(
    name="llm_chat",
    author=[{"name": "FrostN0v0"}],
    version="0.1.0",
    description="群聊会话互动：多轴关系引擎 + 媒体互动 + 可选语音回复",
    role=PluginRole.NORMAL,
)

config = plugin_config(LLMChatConfig)
plug = Plugin.current()

HELP_META = {"icon": "💬", "category": "互动"}

_LOGGER = log.wrapper("[llm_chat]")

registered_tools = register_llm_tools(config)
_LOGGER.info(f"registered LLM tools: {', '.join(registered_tools) or '(none)'}")
register_tag_runtime(config, plug)
register_chat_handler(config, plug)
