"""Opt-in private companion chat through the same ordinary-chat lifecycle."""

from __future__ import annotations

from satori import ChannelType
from sqlalchemy import select
from arclet.entari import Session, MessageCreatedEvent, metadata
from arclet.letoderea import BLOCK, enter_if
from arclet.entari.plugin import PluginRole
from entari_plugin_database import get_session
from arclet.letoderea.context import Contexts
from arclet.entari.plugin.model import Plugin

from . import store
from .. import chat_handler
from ..models import ChatScope
from ..channel_turns import steer_participant_turn

_owner = Plugin.current()
if _owner is not None and _owner.module.__name__ == __name__:
    metadata(
        name="llm_chat_private_companion",
        description="Explicitly enabled private companion chat.",
        role=PluginRole.UTILITY,
    )


async def _enabled_private(session: Session) -> bool:
    if session.channel.type != ChannelType.DIRECT or str(session.user.id) == str(session.account.self_id):
        return False
    async with get_session() as db:
        scope_id = await db.scalar(
            select(ChatScope.id).where(
                ChatScope.platform == str(session.account.platform),
                ChatScope.account_id == str(session.account.self_id),
                ChatScope.channel_id == str(session.channel.id),
            )
        )
    return scope_id is not None and (await store.get_mode(scope_id)).mode == "companion"


async def _should_handle_private(session: Session) -> bool:
    return not chat_handler._is_prefixed_command(session.elements.extract_plain_text()) and await _enabled_private(
        session
    )


if _owner is not None and _owner.module.__name__ == __name__:

    @_owner.dispatch(MessageCreatedEvent).register(priority=900)
    @enter_if(_should_handle_private)
    @steer_participant_turn
    async def on_private_chat(session: Session, ctx: Contexts):
        await chat_handler.handle_chat_turn(session, ctx)
        return BLOCK

    @_owner.dispatch(MessageCreatedEvent).register(priority=999)
    @enter_if(_enabled_private)
    async def claim_private_companion() -> object:
        return BLOCK
