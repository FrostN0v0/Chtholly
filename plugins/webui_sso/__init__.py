"""Optional deployment-layer single sign-on for the native Entari WebUI."""

from arclet.entari import Plugin, BasicConfModel, plugin, metadata, plugin_config
from arclet.entari.plugin import PluginRole


class WebUISsoConfig(BasicConfModel):
    public_origin: str
    auth_url: str = "http://127.0.0.1:4180/oauth2/auth"


plug = Plugin.current()

if plug is not None:
    from fastapi import FastAPI
    import entari_plugin_webui as webui  # entari: plugin
    from entari_plugin_server import get_asgi
    from entari_plugin_webui.api.deps import get_session_store

    from utils.webui_sso import SsoSessions

    from .runtime import install

    metadata(
        name="webui_sso",
        author=[{"name": "FrostN0v0"}],
        version="0.1.0",
        description="Exchange verified deployment SSO sessions for native WebUI sessions",
        role=PluginRole.UTILITY,
        config=WebUISsoConfig,
    )
    config = plugin_config(WebUISsoConfig)
    app = get_asgi()
    if not isinstance(app, FastAPI):
        raise RuntimeError("WebUI SSO requires the native WebUI FastAPI application")
    sessions = SsoSessions(
        public_origin=config.public_origin,
        auth_url=config.auth_url,
        store=get_session_store,
        session_ttl=webui.webui_config.session_ttl,
    )
    plugin.collect_disposes(install(app, sessions))
