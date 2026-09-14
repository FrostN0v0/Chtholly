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

_superuser_check = superusers().check


async def _is_operator(session: Session) -> bool:
    return await _superuser_check(session) is not STOP


from utils.llm_model_core.snapshot import pin_main_model, main_model_scope

from .config import LLMChatConfig
from .identity import resolve_chat_identity, resolve_mentioned_participants
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
from .channel_turns import (
    latest_participant_turn,
    cancel_active_participant_turns,
    current_participant_turn_superseded,
)
from .delivery_audit import delivery_audit_scope, current_delivery_audit
from .chat_evaluation import cancel_pending_evaluations, schedule_relationship_evaluation
from .forward_context import resolve_merged_forward_messages
from .agent_turn_setup import prepare_agent_turn
from .agent_attachments import capture_user_input_images, remove_user_input_attachments
from .reaction_feedback import MessageReactionFeedback, settle_reaction_update, llm_chat_reaction_scope

_LOGGER = log.wrapper("[llm_chat]")
_CHAT_FAILURE_REPLY = "这次回复没有成功，请稍后重试。"
_MEDIA_FAILURE_REPLY = "这次图片处理没有成功，请重新发送原图后再试。"


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


plugin.collect_disposes(cancel_active_participant_turns)
plugin.collect_disposes(cancel_pending_evaluations)


@plug.dispatch(MessageCreatedEvent).register(priority=900)
@enter_if(_should_handle_chat)
@latest_participant_turn
async def on_chat(session: Session, ctx: Contexts):
    reaction = MessageReactionFeedback(session, _LOGGER.warning)
    with delivery_audit_scope(session, _LOGGER.warning), llm_chat_reaction_scope(reaction), main_model_scope():
        try:
            await reaction.set_stage("processing")
            result = await _run_chat(session, ctx, reaction)
            if not reaction.terminal:
                await reaction.finish("failed")
            return result
        except asyncio.CancelledError:
            if current_participant_turn_superseded():
                await settle_reaction_update(reaction.finish("superseded"))
            else:
                await settle_reaction_update(reaction.clear_transient())
            raise
        except Exception:
            await settle_reaction_update(reaction.finish("failed"))
            raise


async def _run_chat(
    session: Session,
    ctx: Contexts,
    reaction: MessageReactionFeedback,
):
    model_text = session.elements.extract_plain_text().strip()
    raw_user_text = model_text
    message_images = collect_message_images(session)
    channel_id = session.channel.id

    try:
        forwarded_messages = await resolve_merged_forward_messages(config, session, _LOGGER.warning)
    except Exception as exc:
        _LOGGER.warning(f"merged forward normalization failed: {type(exc).__name__}")
        forwarded_messages = []

    quoted_message = collect_quoted_message(session)
    if quoted_message is not None:
        forwarded_messages.insert(0, quoted_message)

    if not model_text and not message_images and not forwarded_messages:
        await reaction.finish("failed")
        return BLOCK

    try:
        model_name = pin_main_model(get_model_config(config.model, channel_id)).name
    except ModelNotFoundError as exc:
        _LOGGER.warning(f"channel model resolve failed, using global default: {summarize_exception(exc)}")
        model_name = None
    supports_image_input = model_supports_image_input(model_name)

    try:
        identity = await resolve_chat_identity(session)
    except Exception as exc:
        _LOGGER.warning(f"user identity resolve failed: {summarize_exception(exc)}")
        await session.send(_CHAT_FAILURE_REPLY)
        await reaction.finish("failed")
        return BLOCK
    user_id = identity.user_id
    user_name = identity.display_name
    mentioned_participants = await resolve_mentioned_participants(session)

    current_content: str | list[dict[str, Any]] | None = None
    if supports_image_input:
        current_content, content = await build_multimodal_user_content(
            config,
            session,
            user_name,
            model_text,
            _LOGGER.warning,
            forwarded_messages=forwarded_messages,
            mentioned_participants=mentioned_participants,
        )
    else:
        image_notes = await build_image_notes(config, session, _LOGGER.warning)
        if image_notes:
            model_text = " ".join(part for part in [model_text, *image_notes] if part)
        content = render_forwarded_storage(model_text, forwarded_messages)

    if not content:
        await reaction.finish("failed")
        return BLOCK
    input_attachments = await capture_user_input_images(
        session,
        message_images,
        warn=_LOGGER.warning,
    )
    try:
        prepared = await prepare_agent_turn(
            config,
            session,
            identity,
            model_name=model_name,
            supports_image_input=supports_image_input,
            model_text=model_text,
            raw_user_text=raw_user_text,
            content=content,
            current_content=current_content,
            forwarded_messages=forwarded_messages,
            mentioned_participants=mentioned_participants,
            warn=_LOGGER.warning,
            tool_schemas=registered_tool_schemas,
            input_attachments=input_attachments,
            is_operator=await _is_operator(session),
        )
    except BaseException:
        remove_user_input_attachments(input_attachments)
        raise
    delivery_audit = current_delivery_audit()
    if delivery_audit is not None:
        delivery_audit.bind(prepared.agent_events)
    chat_messages = prepared.chat_messages
    system = prepared.system
    media_requested = prepared.media_requested
    web_limits = prepared.web_limits
    delivery_state = prepared.delivery_state
    channel_image_references = prepared.channel_image_references
    image_edit_references = prepared.image_edit_references
    turn = prepared.lifecycle
    resolution = prepared.resolution
    agent_events = prepared.agent_events
    agent_access = prepared.agent_access
    turn_status = "failed"
    try:
        try:
            await reaction.set_stage("thinking")
            response = await generate_chat_response(
                chat_messages,
                system=system,
                model=model_name,
                channel_id=channel_id,
                ctx=ctx,
                web_limits=web_limits,
                delivery_state=delivery_state,
                channel_image_references=channel_image_references,
                image_edit_references=image_edit_references,
                request_timeout=config.model_request_timeout,
                media_request_timeout=config.media_request_timeout,
                tool_trace=turn.tool_trace,
                agent_events=agent_events,
                agent_access=agent_access,
                resolution=resolution,
            )
        except asyncio.CancelledError:
            turn.capture_tool_events()
            turn_status = "partial" if delivery_state.confirmed_deliveries else "cancelled"
            await turn.preserve_and_rollback()
            if delivery_state.confirmed_deliveries:
                await settle_reaction_update(reaction.finish("partial"))
            raise
        except Exception as exc:
            turn.capture_tool_events()
            _LOGGER.warning(f"llm generate failed: {summarize_exception(exc)}")
            if delivery_state.delivery_attempts:
                await turn.preserve_and_rollback()
                await reaction.finish("partial" if delivery_state.confirmed_deliveries else "failed")
                return BLOCK
            failure_reply = _MEDIA_FAILURE_REPLY if media_requested else _CHAT_FAILURE_REPLY
            try:
                if await turn.deliver_model_reply(session, failure_reply):
                    await turn.persist_delivered_text()
                    turn_status = "failed"
            except asyncio.CancelledError:
                raise
            except Exception as delivery_exc:
                await turn.preserve_and_rollback()
                _LOGGER.warning(f"generation failure notice delivery failed: {summarize_exception(delivery_exc)}")
            await reaction.finish("failed")
            return BLOCK

        turn.capture_tool_events()
        if delivery_state.delivery_attempts > delivery_state.confirmed_deliveries or any(
            event.status in {"failed", "cancelled"} and event.effect in {"partial", "unknown"}
            for event in turn.tool_trace.events
        ):
            await turn.preserve_and_rollback()
            turn_status = "partial" if delivery_state.confirmed_deliveries else "failed"
            await reaction.finish("partial" if delivery_state.confirmed_deliveries else "failed")
            return BLOCK
        if resolution.outcome == "silent":
            turn.agent_events.append(
                "response_decision",
                role="assistant",
                model_visible=False,
                payload={
                    "outcome": "silent",
                    "source": resolution.source,
                    "reason": resolution.reason[:240],
                    "actual_delivery": {
                        "text_messages": 0,
                        "media_messages": 0,
                        "confirmed_deliveries": delivery_state.confirmed_deliveries,
                    },
                },
            )
            turn_status = "silent"
            await reaction.finish_silent()
            return BLOCK
        if resolution.outcome == "declined":
            previous_deliveries = delivery_state.confirmed_deliveries
            if not resolution.reply or not await turn.deliver_model_reply(session, resolution.reply):
                raise RuntimeError("explicit refusal was not confirmed")
            if delivery_state.confirmed_deliveries <= previous_deliveries:
                raise RuntimeError("explicit refusal was suppressed without new delivery")
            await turn.persist_delivered_text()
            turn.agent_events.append(
                "response_decision",
                role="assistant",
                model_visible=False,
                payload={
                    "outcome": "declined",
                    "source": resolution.source,
                    "reason": resolution.reason[:240],
                    "actual_delivery": {
                        "text_messages": len(delivery_state.delivered_texts),
                        "media_messages": delivery_state.confirmed_media_deliveries,
                        "confirmed_deliveries": delivery_state.confirmed_deliveries,
                    },
                },
            )
            turn_status = "declined"
            await reaction.finish("declined")
            return BLOCK
        if resolution.outcome == "delivered":
            if not delivery_state.confirmed_deliveries:
                raise RuntimeError("explicit delivered outcome lacked confirmed output")
            await turn.persist_delivered_text()
            turn.agent_events.append(
                "response_decision",
                role="assistant",
                model_visible=False,
                payload={
                    "outcome": "delivered",
                    "source": resolution.source,
                    "reason": resolution.reason[:240],
                    "actual_delivery": {
                        "text_messages": len(delivery_state.delivered_texts),
                        "media_messages": delivery_state.confirmed_media_deliveries,
                        "confirmed_deliveries": delivery_state.confirmed_deliveries,
                    },
                },
            )
            turn_status = "completed"
            await reaction.finish("success")
            return BLOCK
        try:
            if not await turn.deliver_model_images(session, response):
                await reaction.finish("partial" if delivery_state.confirmed_deliveries else "failed")
                return BLOCK
        except asyncio.CancelledError:
            turn_status = "partial" if delivery_state.confirmed_deliveries else "cancelled"
            raise
        except Exception as exc:
            _LOGGER.warning(f"native image delivery failed: {summarize_exception(exc)}")
            await reaction.finish("partial" if delivery_state.confirmed_deliveries else "failed")
            return BLOCK
        if not await turn.deliver_model_reply(session, response_content(response)):
            await reaction.finish("partial" if delivery_state.confirmed_deliveries else "failed")
            return BLOCK
        await turn.persist_delivered_text()
        turn.agent_events.append(
            "response_decision",
            role="assistant",
            model_visible=False,
            payload={
                "outcome": "automatic",
                "source": "runtime",
                "reason": "",
                "actual_delivery": {
                    "text_messages": len(delivery_state.delivered_texts),
                    "media_messages": delivery_state.confirmed_media_deliveries,
                    "confirmed_deliveries": delivery_state.confirmed_deliveries,
                },
            },
        )
        turn_status = "completed"
        await reaction.finish("success")
        return BLOCK
    except asyncio.CancelledError:
        if turn_status == "failed":
            turn_status = "partial" if delivery_state.confirmed_deliveries else "cancelled"
            await turn.preserve_and_rollback()
        raise
    finally:
        if turn_status not in {"completed", "declined", "silent", "cancelled"} and delivery_state.confirmed_deliveries:
            turn_status = "partial"

        async def finalize_with_delivery_audit() -> None:
            if delivery_audit is not None:
                await delivery_audit.drain()
            await turn.finalize_agent_turn(turn_status)
            if turn_status in {"completed", "silent", "declined"} and turn.agent_turn_id is not None:
                schedule_relationship_evaluation(
                    config,
                    turn_id=turn.agent_turn_id,
                    user_id=user_id,
                    channel_id=channel_id,
                    persona_prompt=prepared.persona.prompt,
                    warn=_LOGGER.warning,
                )

        finalize_task = asyncio.create_task(finalize_with_delivery_audit())
        try:
            await asyncio.shield(finalize_task)
        except asyncio.CancelledError:
            with suppress(asyncio.CancelledError):
                await finalize_task
            raise


@plug.dispatch(MessageCreatedEvent).register(priority=999)
async def block_native_llm_fallback():
    return BLOCK
