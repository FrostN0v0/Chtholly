"""Configuration-gated registration of the independent plugin workshop service tools."""

from __future__ import annotations

from typing import cast

from launart import Launart
from arclet.entari.logger import log
from arclet.entari.plugin.model import PluginDispatcher

from utils.plugin_workshop_core.models import WorkshopAPI

from ._workshop import WorkshopToolContext
from ..core.types import JSONType
from .submit_plugin import register_submit_plugin
from ..core.delivery import DeliveryError
from .activate_plugin import register_activate_plugin
from .rollback_plugin import register_rollback_plugin
from .read_workshop_plugin import register_read_workshop_plugin
from .list_workshop_plugins import register_list_workshop_plugins

_LOGGER = log.wrapper("[llm_chat]")


def _get_workshop() -> WorkshopAPI:
    try:
        return cast(WorkshopAPI, Launart.current().get_component("plugin.workshop"))
    except ValueError:
        raise DeliveryError("The plugin workshop service is unavailable") from None


def register_workshop_tools(
    dispatcher: PluginDispatcher[JSONType],
    *,
    enabled: bool,
    runtime: WorkshopToolContext | None = None,
) -> list[str]:
    if not enabled:
        return []
    context = runtime or WorkshopToolContext(get_service=_get_workshop, warn=_LOGGER.warning)
    register_list_workshop_plugins(dispatcher, context)
    register_read_workshop_plugin(dispatcher, context)
    register_submit_plugin(dispatcher, context)
    register_activate_plugin(dispatcher, context)
    register_rollback_plugin(dispatcher, context)
    return ["list_workshop_plugins", "read_workshop_plugin", "submit_plugin", "activate_plugin", "rollback_plugin"]
