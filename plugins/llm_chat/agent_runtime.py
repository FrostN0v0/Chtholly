"""Database-ready migrations and hot-reload-safe relationship worker startup."""

from __future__ import annotations

import asyncio

from launart import Launart
from arclet.entari import Ready, plugin, plugin_config
from arclet.entari.logger import log
from arclet.entari.event.plugin import PluginLoadedSuccess

from .config import LLMChatConfig
from .agent_migration import migrate_legacy_agent_events
from .chat_evaluation import resume_relationship_evaluations
from .relationships.recovery import recover_unqueued_evidence
from .relationships.migration import initialize_relationship_store

_LOGGER = log.wrapper("[llm_chat.agent]")
_CONFIG = plugin_config(LLMChatConfig)
_INITIALIZE_LOCK = asyncio.Lock()
_initialized = False


async def _initialize_store() -> None:
    global _initialized
    async with _INITIALIZE_LOCK:
        if _initialized:
            return
        imported = await migrate_legacy_agent_events()
        await initialize_relationship_store()
        recovered = await recover_unqueued_evidence(_CONFIG.eval_model or _CONFIG.model or "default")
        await resume_relationship_evaluations(_CONFIG, _LOGGER.warning)
        _initialized = True
        if imported:
            _LOGGER.info(f"imported {imported} legacy conversation rows into agent sessions")
        if recovered:
            _LOGGER.info(f"recovered {recovered} completed relationship interactions")


@plugin.listen(Ready, priority=-90)
async def initialize_agent_event_store() -> None:
    database = Launart.current().get_component("database/sqlalchemy")
    await database.status.wait_for("blocking")
    await _initialize_store()


@plugin.listen(PluginLoadedSuccess)
async def resume_after_plugin_reload(event: PluginLoadedSuccess) -> None:
    if event.plugin_id.rsplit(".", 1)[-1] != "llm_chat":
        return
    try:
        database = Launart.current().get_component("database/sqlalchemy")
    except (LookupError, ValueError):
        return
    if database.status.blocking:
        await _initialize_store()
