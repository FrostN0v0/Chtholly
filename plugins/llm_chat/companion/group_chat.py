"""Opt-in ambient group admission before the unchanged addressed-chat handler."""

from __future__ import annotations

from satori import ChannelType
from arclet.entari import Session, MessageCreatedEvent, metadata
from arclet.letoderea import BLOCK
from arclet.entari.plugin import PluginRole
from arclet.entari.event.base import MessageDeletedEvent, MessageUpdatedEvent
from arclet.letoderea.context import Contexts
from arclet.entari.plugin.model import Plugin

from .. import chat_handler
from .runtime import current_companion_service

_owner = Plugin.current()
if _owner is not None and _owner.module.__name__ == __name__:
    metadata(
        name="llm_chat_group_companion",
        description="Scoped ambient group conversation in explicitly enabled companion mode.",
        role=PluginRole.UTILITY,
    )


async def admit_group_message(
    session: Session,
    ctx: Contexts,
    is_reply_me: bool = False,
    is_notice_me: bool = False,
) -> bool:
    if session.channel.type != ChannelType.TEXT or str(session.user.id) == str(session.account.self_id):
        return False
    if chat_handler._is_prefixed_command(session.elements.extract_plain_text()):
        return False
    runtime = current_companion_service()
    if runtime is None:
        return False
    directed = await chat_handler._addressed_to_me(session, is_reply_me, is_notice_me)
    return await runtime.admit_dialogue(session, ctx, directed=directed)


if _owner is not None and _owner.module.__name__ == __name__:

    @_owner.dispatch(MessageCreatedEvent).register(priority=850)
    async def on_group_dialogue(
        session: Session,
        ctx: Contexts,
        is_reply_me: bool = False,
        is_notice_me: bool = False,
    ):
        if await admit_group_message(session, ctx, is_reply_me, is_notice_me):
            return BLOCK
        return None

    @_owner.dispatch(MessageUpdatedEvent).register(priority=100)
    async def on_dialogue_update(
        session: Session, ctx: Contexts, is_reply_me: bool = False, is_notice_me: bool = False
    ) -> None:
        runtime = current_companion_service()
        if runtime is None:
            return
        directed = session.channel.type == ChannelType.DIRECT or await chat_handler._addressed_to_me(
            session, is_reply_me, is_notice_me
        )
        await runtime.revise_dialogue(
            session,
            ctx,
            directed=directed,
            deleted=chat_handler._is_prefixed_command(session.elements.extract_plain_text()),
        )

    @_owner.dispatch(MessageDeletedEvent).register(priority=100)
    async def on_dialogue_delete(session: Session, ctx: Contexts) -> None:
        runtime = current_companion_service()
        if runtime is not None:
            await runtime.revise_dialogue(session, ctx, deleted=True)
