"""Bounded public-channel perception service."""

from typing import Annotated

from arclet.entari import Ready, plugin, metadata, add_service, plugin_config
from arclet.entari.core import on_stage
from arclet.entari.plugin import PluginRole
from graia.amnesia.builtins.sqla import SqlalchemyService

from .config import ChannelPerceptionConfig
from .service import ChannelPerceptionService

metadata(
    name="channel_perception",
    author=[{"name": "FrostN0v0"}],
    version="0.1.0",
    description="Bounded participant and ambient-message perception",
    role=PluginRole.UTILITY,
    config=ChannelPerceptionConfig,
)

_config = plugin_config(ChannelPerceptionConfig)
channel_perception = ChannelPerceptionService(_config)
add_service(channel_perception)


@plugin.listen(Ready, priority=-100)
async def wait_for_database_tables(database: Annotated[SqlalchemyService, on_stage("blocking")]) -> None:
    """Gate later Ready handlers on completed database table initialization."""


from . import (
    models as models,  # noqa: E402  # entari: package
    listener as listener,  # noqa: E402  # entari: package
)

__all__ = ["ChannelPerceptionService", "channel_perception"]
