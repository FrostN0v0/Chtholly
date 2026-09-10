"""Safe native WebUI saves with model hot updates and managed restart fallback."""

from __future__ import annotations

from arclet.entari import plugin, metadata
from arclet.entari.plugin import PluginRole
from arclet.entari.plugin.model import Plugin

plug = Plugin.current()

if plug is not None:
    from fastapi import FastAPI
    from entari_plugin_server import get_asgi
    from arclet.entari.config.file import EntariConfig

    from .api import install_api
    from .control import file_digest
    from .serializer import install_dumper

    metadata(
        name="webui_config_apply",
        author=[{"name": "FrostN0v0"}],
        version="0.1.0",
        description="Safe WebUI saves with atomic model hot updates and verified restart fallback",
        role=PluginRole.UTILITY,
    )
    app = get_asgi()
    if not isinstance(app, FastAPI):
        raise RuntimeError("Config apply requires entari-plugin-webui to own the FastAPI application")
    running_sha256 = file_digest(EntariConfig.instance.path)
    plugin.collect_disposes(install_dumper())
    plugin.collect_disposes(install_api(app, running_sha256))
