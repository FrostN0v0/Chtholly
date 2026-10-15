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

try:
    plug = Plugin.current()
except LookupError:
    plug = None


def _configure_litellm_logging() -> None:
    previous = litellm.suppress_debug_info
    litellm.suppress_debug_info = True

    def restore() -> None:
        if litellm.suppress_debug_info is True:
            litellm.suppress_debug_info = previous

    plugin.collect_disposes(restore)


if plug is not None and plug.module.__name__ == __name__:
    import channel_perception as channel_perception  # entari: plugin

    _configure_litellm_logging()
    from .agno_compat import install_agno_tool_bridge  # entari: package
    from .protocol_compat import install_entari_internal_bridge  # entari: package
    from .delivery_audit import install_delivery_audit  # entari: package
    from .group_delivery import install_group_delivery  # entari: package

    install_agno_tool_bridge()
    plugin.collect_disposes(install_entari_internal_bridge())
    plugin.collect_disposes(install_delivery_audit())
    plugin.collect_disposes(install_group_delivery())
    from .config import LLMChatConfig  # entari: package
    from .config_schema import localize_llm_chat_schema  # entari: package

    metadata(
        name="llm_chat",
        author=[{"name": "FrostN0v0"}],
        version="0.1.0",
        description="群聊会话互动：多轴关系引擎 + 媒体互动 + 可选语音回复",
        role=PluginRole.NORMAL,
        config=LLMChatConfig,
    )
    plug.register_schema(fragment=localize_llm_chat_schema, replace=True)
    from . import model_state_runtime as model_state_runtime  # entari: package

    from . import tool_runtime as tool_runtime  # entari: package
    from . import reminders as reminders  # entari: subplugin
    from . import chat_handler as chat_handler  # entari: package
    from arclet.entari import add_service
    from arclet.entari.event.lifespan import AccountUpdate
    from arclet.entari.scheduler import schedule, timer
    from .reminders.service import ReminderService  # entari: package
    from .companion.service import CompanionService  # entari: package
    from .companion.runtime import install_companion_runtime  # entari: package

    registered_tool_schemas = [
        *getattr(tool_runtime, "registered_tool_schemas", ()),
        *getattr(reminders, "registered_tool_schemas", ()),
    ]

    reminder_service = add_service(ReminderService(chat_handler.config, tool_schemas=registered_tool_schemas))
    plugin.collect_disposes(reminder_service.aclose)
    companion_service = add_service(CompanionService(chat_handler.config, tool_schemas=registered_tool_schemas))
    plugin.collect_disposes(install_companion_runtime(companion_service))
    plugin.collect_disposes(companion_service.aclose)

    @plugin.listen(AccountUpdate)
    async def wake_reminders_for_account(event: AccountUpdate) -> None:
        reminder_service.account_update(event.account)
        companion_service.account_update(event.account)

    @schedule(timer.every_seconds(5))
    async def wake_reminder_heartbeat() -> None:
        reminder_service.wake()
        companion_service.wake()

    from . import agent_command as agent_command  # entari: package
    from . import reminder_command as reminder_command  # entari: subplugin

    for child_name in ("command", "private_chat"):
        module_name = f"{__name__}.companion.{child_name}"
        plugin.package(module_name)
        if not plugin.load_plugin(module_name, config={"$filter": ""}):
            raise RuntimeError(f"Companion child plugin failed to load: {child_name}")

    from . import agent_runtime as agent_runtime  # entari: package
    from . import tag_runtime as tag_runtime  # entari: package
    from . import meme_webui as meme_webui  # entari: package
    from . import agent_webui as agent_webui  # entari: package
    from . import reminder_webui as reminder_webui  # entari: package

    registered_tools = [*tool_runtime.registered_tools, *getattr(reminders, "registered_tools", ())]
    _LOGGER.info(f"registered LLM tools: {', '.join(registered_tools) or '(none)'}")
