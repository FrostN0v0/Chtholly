"""Native per-scope opt-in companion mode commands."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from arclet.entari import Session, command, metadata
from arclet.alconna import Args
from arclet.letoderea import STOP
from arclet.entari.filter import superusers
from arclet.entari.plugin import PluginRole
from entari_plugin_database import get_session
from arclet.entari.plugin.model import Plugin

from . import store
from ..models import ChatScope
from .runtime import current_companion_service
from ..identity import resolve_chat_identity
from ..personality import persona_scope_lock, select_scope_persona, resolve_scope_persona
from ..agent_context import current_agent_access
from ..session_manager import scope_identity, get_or_create_scope, resolve_scope_identity

_owner = Plugin.current()
if _owner is not None and _owner.module.__name__ == __name__:
    metadata(name="llm_chat_modes", description="Native opt-in companion mode control.", role=PluginRole.UTILITY)

_superuser = superusers().check
_LABELS = {"normal": "\u666e\u901a\u5bf9\u8bdd", "companion": "\u81ea\u4e3b\u966a\u4f34"}


async def _existing_scope(session: Session) -> ChatScope | None:
    identity = scope_identity(session)
    async with get_session() as db:
        return await db.scalar(
            select(ChatScope).where(
                ChatScope.platform == identity.platform,
                ChatScope.account_id == identity.account_id,
                ChatScope.channel_id == identity.channel_id,
            )
        )


@command.on("llmchat mode {requested_mode}", args={"requested_mode": Args["requested_mode?", str]})
async def conversation_mode(session: Session, requested_mode: str = "") -> str:
    """Query without model calls; mutation requires a direct operator command."""
    mode = requested_mode
    if not mode:
        scope = await _existing_scope(session)
        current = await store.get_mode(scope.id, session_factory=get_session) if scope else None
        label = _LABELS[current.mode if current else "normal"]
        return "\u5f53\u524d\u6a21\u5f0f\uff1a" + label
    if mode not in _LABELS:
        return "\u7528\u6cd5\uff1a/llmchat mode [normal|companion]"
    if current_agent_access() is not None:
        return (
            "\u8bf7\u7531\u7ba1\u7406\u5458\u76f4\u63a5\u53d1\u9001\u6a21\u5f0f\u6307\u4ee4\uff0c"
            "\u6a21\u578b\u5de5\u5177\u4e0d\u80fd\u81ea\u884c\u542f\u7528\u3002"
        )
    if await _superuser(session) is STOP:
        return (
            "\u6743\u9650\u4e0d\u8db3\uff1a\u4ec5\u914d\u7f6e\u7684\u8d85\u7ea7\u7528\u6237"
            "\u53ef\u5207\u6362\u5bf9\u8bdd\u6a21\u5f0f\u3002"
        )
    runtime = current_companion_service()
    if runtime is None:
        return "\u81ea\u4e3b\u966a\u4f34\u670d\u52a1\u5c1a\u672a\u5c31\u7eea\uff0c\u672a\u4fee\u6539\u6a21\u5f0f\u3002"
    if mode == "normal" and await _existing_scope(session) is None:
        return "\u5f53\u524d\u5df2\u662f\u666e\u901a\u5bf9\u8bdd\uff0c\u672a\u521b\u5efa\u65b0\u72b6\u6001\u3002"
    scope = await get_or_create_scope(await resolve_scope_identity(session))
    async with persona_scope_lock(scope.id):
        persona = await resolve_scope_persona(runtime.config, scope.id)
        await select_scope_persona(runtime.config, scope.id, persona.key)
        actor = await resolve_chat_identity(session) if mode == "companion" else None
        previous = await store.get_mode(scope.id)
        current = await store.set_mode(
            scope.id,
            mode,
            persona_key=persona.key,
            now=datetime.now(timezone.utc),
            expected_revision=previous.revision,
        )
        if current.revision != previous.revision:
            await runtime.cancel_scope(scope.id)
        if actor is not None:
            await store.register_member(
                scope.id,
                persona.key,
                user_id=actor.user_id,
                platform_user_id=str(session.user.id),
                display_name=actor.display_name,
                now=datetime.now(timezone.utc),
            )
    runtime.wake()
    if current.revision == previous.revision:
        return "\u5f53\u524d\u5df2\u662f" + _LABELS[mode] + "\uff0c\u672a\u91cd\u590d\u542f\u52a8\u3002"
    if mode == "normal":
        return (
            "\u5df2\u5207\u6362\u4e3a\u666e\u901a\u5bf9\u8bdd\uff1b\u81ea\u4e3b\u6d3b\u52a8\u548c"
            "\u8054\u7cfb\u5df2\u505c\u6b62\uff0c\u72b6\u6001\u4fdd\u7559\uff0c\u660e\u786e\u63d0"
            "\u9192\u4e0d\u53d7\u5f71\u54cd\u3002"
        )
    return (
        "\u5df2\u5728\u5f53\u524d\u5bf9\u8bdd\u542f\u7528\u81ea\u4e3b\u966a\u4f34\uff1b\u5141"
        "\u8bb8\u89d2\u8272\u6d3b\u52a8\u3001\u6301\u7eed\u5173\u6ce8\u4e0e\u53d7\u9650\u4e3b"
        "\u52a8\u8054\u7cfb\u3002"
    )
