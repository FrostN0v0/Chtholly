"""Safe WebUI persistence and managed full-process configuration application."""

from __future__ import annotations

from fastapi import FastAPI
from arclet.entari import plugin, metadata
from arclet.entari.plugin import PluginRole
from entari_plugin_server import get_asgi
from arclet.entari.config.file import EntariConfig
from arclet.entari.plugin.model import Plugin

from . import ui
from .api import install_api
from .control import file_digest
from .serializer import install_dumper

plug = Plugin.current()

if plug is not None:
    metadata(
        name="webui_config_apply",
        author=[{"name": "FrostN0v0"}],
        version="0.1.0",
        description="Safe WebUI saves with health-checked full-process configuration application",
        role=PluginRole.UTILITY,
    )
    app = get_asgi()
    if not isinstance(app, FastAPI):
        raise RuntimeError("Config apply requires entari-plugin-webui to own the FastAPI application")
    running_sha256 = file_digest(EntariConfig.instance.path)
    plugin.collect_disposes(install_dumper())
    plugin.collect_disposes(install_api(app, running_sha256))
    ui.install_ui(app, plug)
