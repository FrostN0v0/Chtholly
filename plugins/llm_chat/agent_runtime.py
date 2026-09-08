"""Database-ready AgentEvent migration hook."""

from launart import Launart
from arclet.entari import Ready, plugin
from arclet.entari.logger import log

from .agent_migration import migrate_legacy_agent_events

_LOGGER = log.wrapper("[llm_chat.agent]")


@plugin.listen(Ready, priority=-90)
async def initialize_agent_event_store() -> None:
    database = Launart.current().get_component("database/sqlalchemy")
    await database.status.wait_for("blocking")
    imported = await migrate_legacy_agent_events()
    if imported:
        _LOGGER.info(f"imported {imported} legacy conversation rows into agent sessions")
