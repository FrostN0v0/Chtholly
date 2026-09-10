"""Entari-owned entrypoint for the reviewed native plugin workshop."""

from arclet.entari import Ready, Plugin, plugin, metadata, local_data, add_service, plugin_config
from arclet.entari.plugin import PluginRole

try:
    _owner = Plugin.current()
except LookupError:
    _owner = None

if _owner is not None and _owner.module.__name__ == __name__:
    from .config import WorkshopConfig  # entari: package
    from .service import PluginWorkshopService  # entari: package

    metadata(
        name="plugin_workshop",
        author=[{"name": "FrostN0v0"}],
        version="1.0.0",
        description="Isolated functional acceptance and operator-approved native plugin lifecycle",
        role=PluginRole.UTILITY,
        config=WorkshopConfig,
    )
    workshop = PluginWorkshopService(plugin_config(WorkshopConfig), root=local_data.get_data_dir("plugin_workshop"))
    add_service(workshop)
    plugin.collect_disposes(workshop.aclose)

    from .webui import register_webui  # entari: package
    from .commands import register_commands  # entari: package

    register_commands(workshop)
    register_webui(workshop)

    @plugin.listen(Ready, priority=1000)
    async def restore_committed_workshop_plugins() -> None:
        # Initial application command registration precedes Ready. Preparing must
        # not await this event: the plugin manager emits it only in blocking.
        await workshop.restore()
