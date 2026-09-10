"""Require actual native WebUI password authentication for privileged review."""

from arclet.entari import Plugin, Startup

_owner = Plugin.current()


def register_password_authentication() -> None:
    import entari_plugin_webui as webui  # entari: plugin
    from entari_plugin_webui.api.deps import get_session_store
    from entari_plugin_webui.core.security import (
        hash_password,
        is_local_mode,
        set_local_mode,
        is_hashed_password,
    )

    def require_password() -> None:
        password = webui.webui_config.password
        if not password:
            raise RuntimeError("Plugin workshop requires an explicitly configured WebUI password")
        # Never promote cookies created by passwordless login into administrator sessions.
        if is_local_mode() and get_session_store().count():
            raise RuntimeError("Restart the Bot to invalidate existing passwordless WebUI sessions")
        if not is_hashed_password(password):
            webui.webui_config.password = hash_password(password)
        set_local_mode(False)

    require_password()
    if _owner is None:
        raise RuntimeError("Workshop authentication requires its native plugin owner")

    @_owner.dispatch(Startup).register(priority=200)
    def enforce_password_after_webui_startup() -> None:
        # Upstream startup unconditionally enables passwordless mode for loopback hosts.
        require_password()
