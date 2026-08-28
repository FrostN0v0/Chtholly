"""Message-created handler for llm_chat."""

from __future__ import annotations

import re
from typing import Any
import asyncio
from contextlib import suppress

from arclet.entari import At, Session, MessageCreatedEvent, plugin, plugin_config
from arclet.letoderea import STOP, BLOCK, enter_if
from arclet.entari.config import EntariConfig
from arclet.entari.filter import superusers
from arclet.entari.logger import log
from arclet.letoderea.context import Contexts
from entari_plugin_llm.config import get_model_config
from arclet.entari.plugin.model import Plugin
from entari_plugin_llm.exception import ModelNotFoundError

from .config import LLMChatConfig
from .identity import resolve_chat_identity
from .core.media import has_meaningful_text
from .generation import response_content, generate_chat_response
from .core.errors import summarize_exception
from .chat_context import (
    build_image_notes,
    collect_message_images,
    collect_quoted_message,
    model_supports_image_input,
    build_multimodal_user_content,
)
from .core.forward import render_forwarded_storage
from .tool_runtime import registered_tool_schemas
from .channel_turns import latest_channel_turn, cancel_active_channel_turns
from .chat_evaluation import cancel_pending_evaluations, schedule_chat_state_after_delivery
from .core.engagement import turn_feedback
from .forward_context import resolve_merged_forward_messages
from .agent_turn_setup import prepare_agent_turn
from .engagement_state import record_declined_turn, persist_turn_feedback

_LOGGER = log.wrapper("[llm_chat]")
_CHAT_FAILURE_REPLY = "这次回复没有成功，请稍后重试。"
_MEDIA_FAILURE_REPLY = "这次图片处理没有成功，请重新发送原图后再试。"
_superuser_check = superusers().check


async def _is_operator(session: Session) -> bool:
    """Operators are always answered regardless of relationship state."""

    return await _superuser_check(session) is not STOP


async def _addressed_to_me(session: Session, is_reply_me: bool = False, is_notice_me: bool = False) -> bool:
    """Accept explicit mentions/replies plus At(bot) at any position."""
    if is_reply_me or is_notice_me:
        return True
    self_id = session.account.self_id
    return any(at.id == self_id for at in session.elements.select(At) if at.id)


def _is_prefixed_command(text: str) -> bool:
    stripped = text.lstrip()
    basic = EntariConfig.instance.basic
    if any(prefix and stripped.startswith(prefix) for prefix in basic.prefix):
        return True
    nickname = basic.nickname.strip()
    return bool(nickname and re.match(rf"^@?{re.escape(nickname)}[，,:\s]+", stripped))


async def _should_handle_chat(
    session: Session,
    is_reply_me: bool = False,
    is_notice_me: bool = False,
) -> bool:
    if _is_prefixed_command(session.elements.extract_plain_text()):
        return False
    return await _addressed_to_me(session, is_reply_me, is_notice_me)


config = plugin_config(LLMChatConfig)
plug = Plugin.current()


plugin.collect_disposes(cancel_active_channel_turns)
plugin.collect_disposes(cancel_pending_evaluations)


@plug.dispatch(MessageCreatedEvent).register(priority=900)
@enter_if(_should_handle_chat)
@latest_channel_turn
async def on_chat(session: Session, ctx: Contexts):
    model_text = session.elements.extract_plain_text().strip()
    require_text_reply = bool(collect_message_images(session)) and not has_meaningful_text(model_text)
    channel_id = session.channel.id
    user_name = str((session.member.nick if session.member else None) or session.user.name or session.user.id).strip()

    try:
        forwarded_messages = await resolve_merged_forward_messages(config, session, _LOGGER.warning)
    except Exception as exc:
        _LOGGER.warning(f"merged forward normalization failed: {type(exc).__name__}")
        forwarded_messages = []

    quoted_message = collect_quoted_message(session)
    if quoted_message is not None:
        forwarded_messages.insert(0, quoted_message)

    try:
        model_name = get_model_config(config.model, channel_id).name
    except ModelNotFoundError as exc:
        _LOGGER.warning(f"channel model resolve failed, using global default: {summarize_exception(exc)}")
        model_name = None
    supports_image_input = model_supports_image_input(model_name)

    current_content: str | list[dict[str, Any]] | None = None
    if supports_image_input:
        current_content, content = await build_multimodal_user_content(
            config,
            session,
            user_name,
            model_text,
            _LOGGER.warning,
            forwarded_messages=forwarded_messages,
        )
    else:
        image_notes = await build_image_notes(config, session, _LOGGER.warning)
        if image_notes:
            model_text = " ".join(part for part in [model_text, *image_notes] if part)
        content = render_forwarded_storage(model_text, forwarded_messages)

    if not content:
        return BLOCK
    try:
        identity = await resolve_chat_identity(session)
    except Exception as exc:
        _LOGGER.warning(f"user identity resolve failed: {summarize_exception(exc)}")
        await session.send(_CHAT_FAILURE_REPLY)
        return BLOCK
    user_id = identity.user_id
    user_name = identity.display_name
    prepared = await prepare_agent_turn(
        config,
        session,
        identity,
        model_name=model_name,
        supports_image_input=supports_image_input,
        model_text=model_text,
        content=content,
        current_content=current_content,
        forwarded_messages=forwarded_messages,
        warn=_LOGGER.warning,
        tool_schemas=registered_tool_schemas,
        requires_media_reply=require_text_reply,
        is_operator=await _is_operator(session),
    )
    memory_context = prepared.memory_context
    eval_history = prepared.eval_history
    chat_messages = prepared.chat_messages
    system = prepared.system
    media_requested = prepared.media_requested
    web_limits = prepared.web_limits
    delivery_state = prepared.delivery_state
    channel_image_references = prepared.channel_image_references
    turn = prepared.lifecycle
    engagement = prepared.engagement
    feedback = turn_feedback(prepared.engagement_signals, declined=not engagement.replies)
    if not engagement.replies:
        # Deliberate silence is a normal terminal state, not a generation failure.
        await record_declined_turn(channel_id, user_id, user_name)
        await persist_turn_feedback(
            user_id=user_id,
            channel_id=channel_id,
            feedback=feedback,
        )
        await turn.finalize_agent_turn("declined")
        _LOGGER.info(f"engagement declined reply: {'; '.join(engagement.reasons)}")
        return BLOCK
    agent_events = prepared.agent_events
    agent_access = prepared.agent_access
    turn_status = "failed"
    try:
        try:
            response = await generate_chat_response(
                chat_messages,
                system=system,
                model=model_name,
                channel_id=channel_id,
                ctx=ctx,
                web_limits=web_limits,
                delivery_state=delivery_state,
                channel_image_references=channel_image_references,
                request_timeout=config.model_request_timeout,
                media_request_timeout=config.media_request_timeout,
                tool_trace=turn.tool_trace,
                agent_events=agent_events,
                agent_access=agent_access,
                require_text_reply=require_text_reply,
            )
        except asyncio.CancelledError:
            turn.capture_tool_events()
            turn_status = "partial" if delivery_state.confirmed_deliveries else "cancelled"
            await turn.preserve_and_rollback()
            raise
        except Exception as exc:
            turn.capture_tool_events()
            _LOGGER.warning(f"llm generate failed: {summarize_exception(exc)}")
            if delivery_state.delivery_attempts:
                await turn.preserve_and_rollback()
                return BLOCK
            failure_reply = _MEDIA_FAILURE_REPLY if media_requested else _CHAT_FAILURE_REPLY
            try:
                if await turn.deliver_model_reply(session, failure_reply):
                    await turn.persist_delivered_text()
                    turn_status = "completed"
            except asyncio.CancelledError:
                raise
            except Exception as delivery_exc:
                await turn.preserve_and_rollback()
                _LOGGER.warning(f"generation failure notice delivery failed: {summarize_exception(delivery_exc)}")
            return BLOCK

        turn.capture_tool_events()
        try:
            if not await turn.deliver_model_images(session, response):
                return BLOCK
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _LOGGER.warning(f"native image delivery failed: {summarize_exception(exc)}")
            return BLOCK
        if not await turn.deliver_model_reply(session, response_content(response)):
            return BLOCK
        assistant_reply = await turn.persist_delivered_text()
        turn_status = "completed"
        await persist_turn_feedback(
            user_id=user_id,
            channel_id=channel_id,
            feedback=feedback,
        )
        schedule_chat_state_after_delivery(
            config,
            memory_context,
            eval_history,
            user_id=user_id,
            user_name=user_name,
            channel_id=channel_id,
            user_content=content,
            assistant_reply=assistant_reply,
            warn=_LOGGER.warning,
        )
        return BLOCK
    finally:
        if turn_status not in {"completed", "cancelled"} and delivery_state.confirmed_deliveries:
            turn_status = "partial"
        finalize_task = asyncio.create_task(turn.finalize_agent_turn(turn_status))
        try:
            await asyncio.shield(finalize_task)
        except asyncio.CancelledError:
            with suppress(asyncio.CancelledError):
                await finalize_task
            raise


@plug.dispatch(MessageCreatedEvent).register(priority=999)
async def block_native_llm_fallback():
    return BLOCK
