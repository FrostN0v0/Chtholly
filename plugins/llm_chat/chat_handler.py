"""Message-created handler for llm_chat."""

from __future__ import annotations

import re
from typing import Any, cast
import asyncio
from datetime import datetime

from arclet.entari import At, Session, MessageCreatedEvent, plugin_config
from arclet.letoderea import BLOCK, enter_if
from arclet.entari.config import EntariConfig
from arclet.entari.logger import log
from arclet.letoderea.context import Contexts
from entari_plugin_llm.config import get_model_config
from arclet.entari.plugin.model import Plugin
from entari_plugin_llm.exception import ModelNotFoundError

from .config import LLMChatConfig
from .identity import resolve_chat_identity
from .core.eval import apply_deltas
from .core.types import ChatMessage
from .generation import response_content, generate_chat_response
from .web.policy import normalize_web_access_limits
from .core.errors import summarize_exception
from .chat_context import (
    build_image_notes,
    build_chat_messages,
    collect_quoted_message,
    build_eval_conversation,
    model_supports_image_input,
    build_multimodal_user_content,
    requests_recent_channel_context,
)
from .core.compose import energy_at, compose_persona_prompt
from .core.forward import render_forwarded_storage
from .tool_history import persist_tool_events, load_recent_tool_activity
from .core.delivery import DeliveryState, normalize_delivery_limits
from .persona.store import (
    get_mood,
    set_mood,
    get_relation,
    load_history,
    save_relation,
    append_message,
    delete_message,
)
from .channel_images import ChannelImageReferences
from .persona.runner import run_evaluation
from .turn_lifecycle import ActiveChatTurn
from .forward_context import resolve_merged_forward_messages
from .core.media_delivery import latest_user_requests_media, latest_user_requests_image_generation
from .core.self_reference import append_self_reference_image
from .persona.memory_update import apply_memory_updates
from .persona.memory_context import load_memory_context

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


@plug.dispatch(MessageCreatedEvent).register(priority=900)
@enter_if(_should_handle_chat)
async def on_chat(session: Session, ctx: Contexts):
    model_text = session.elements.extract_plain_text().strip()
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
    channel_image_references = ChannelImageReferences()

    rel = await get_relation(user_id, channel_id)
    mood = await get_mood(channel_id)
    energy = energy_at(datetime.now().hour)
    memory_context = await load_memory_context(config, user_id, channel_id, content)
    history = await load_history(channel_id, config.context_window)
    try:
        recent_tool_activity = await load_recent_tool_activity(
            channel_id,
            history,
            max_events=config.tool_context_max_events,
            max_chars=config.tool_context_max_chars,
        )
    except Exception as exc:
        _LOGGER.warning(f"tool history load failed: {summarize_exception(exc)}")
        recent_tool_activity = []
    generation_history = [] if requests_recent_channel_context(model_text) else history
    messages = build_chat_messages(
        generation_history,
        user_name,
        model_text,
        current_content,
        forwarded_messages,
    )
    chat_messages = cast(list[ChatMessage], messages)
    self_reference_attached = False
    if supports_image_input and latest_user_requests_image_generation(chat_messages):
        self_reference_attached = append_self_reference_image(
            chat_messages,
            config.self_reference_image,
            _LOGGER.warning,
        )
    media_requested = latest_user_requests_media(chat_messages)
    web_limits = normalize_web_access_limits(
        config.web_search_max_calls_per_generation,
        config.web_page_max_calls_per_generation,
        config.web_total_max_calls_per_generation,
    )
    delivery_limits = normalize_delivery_limits(
        config.delivery_min_interval_seconds,
        config.delivery_default_interval_seconds,
        config.delivery_max_interval_seconds,
        config.delivery_max_text_messages_per_generation,
        config.delivery_max_text_chars_per_message,
        config.delivery_max_forward_nodes,
        config.delivery_max_forward_chars_per_node,
        config.delivery_max_total_text_chars_per_generation,
        config.delivery_max_media_messages_per_generation,
    )
    delivery_state = DeliveryState(limits=delivery_limits)
    system = compose_persona_prompt(
        config.persona,
        mood,
        energy,
        affection=rel.affection,
        trust=rel.trust,
        dependence=rel.dependence,
        resentment=rel.resentment,
        familiarity=rel.familiarity,
        impression=rel.impression,
        profile=memory_context.chat_profile,
        relevant_memories=memory_context.relevant_memories,
        recent_tool_activity=recent_tool_activity,
        user_name=user_name,
        current_participant_ref=identity.participant_ref,
        self_reference_attached=self_reference_attached,
        web_search_limit=web_limits.search_limit,
        web_page_limit=web_limits.read_limit,
        web_total_limit=web_limits.total_limit,
        delivery_limits=delivery_limits,
    )

    user_message_id = await append_message(channel_id, user_id, user_name, "user", content)

    turn = ActiveChatTurn(
        channel_id=channel_id,
        user_message_id=user_message_id,
        delivery_state=delivery_state,
        append_history=append_message,
        delete_history=delete_message,
        warn=lambda message: _LOGGER.warning(message),
        persist_tool_events=persist_tool_events,
        tool_history_retention=config.tool_history_max_records_per_channel,
    )
    try:
        try:
            response = await generate_chat_response(
                cast(list[ChatMessage], messages),
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
            )
        finally:
            await turn.persist_tool_trace()
    except asyncio.CancelledError:
        await turn.preserve_and_rollback()
        raise
    except Exception as exc:
        _LOGGER.warning(f"llm generate failed: {summarize_exception(exc)}")
        if delivery_state.delivery_attempts:
            await turn.preserve_and_rollback()
            return BLOCK
        failure_reply = _MEDIA_FAILURE_REPLY if media_requested else _CHAT_FAILURE_REPLY
        try:
            if await turn.deliver_model_reply(session, failure_reply):
                await turn.persist_delivered_text()
        except asyncio.CancelledError:
            raise
        except Exception as delivery_exc:
            await turn.preserve_and_rollback()
            _LOGGER.warning(f"generation failure notice delivery failed: {summarize_exception(delivery_exc)}")
        return BLOCK

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
    familiarity = min(100.0, rel.familiarity + 1)
    axes = {
        "affection": rel.affection,
        "trust": rel.trust,
        "dependence": rel.dependence,
        "resentment": rel.resentment,
    }
    impression = rel.impression
    counter = rel.eval_counter + 1

    if counter >= config.eval_every_n:
        counter = 0
        recent = history[-config.eval_context_window :] if config.eval_context_window > 0 else []
        conversation = build_eval_conversation(recent, user_id, user_name, content, assistant_reply)
        try:
            result = await run_evaluation(
                config,
                config.persona,
                axes,
                impression,
                memory_context.evaluator_profile_facts,
                conversation,
                user_name,
                channel_id,
            )
        except Exception as exc:
            _LOGGER.warning(f"relationship evaluation failed: {summarize_exception(exc)}")
            result = None
        if result is not None:
            axes = apply_deltas(axes, result)
            impression = result.impression
            try:
                await apply_memory_updates(config, user_id, channel_id, result)
            except Exception as exc:
                _LOGGER.warning(f"memory update failed: {summarize_exception(exc)}")
            await set_mood(channel_id, mood + result.mood_delta)

    await save_relation(
        user_id,
        channel_id,
        axes=axes,
        impression=impression,
        familiarity=familiarity,
        eval_counter=counter,
    )
    return BLOCK


@plug.dispatch(MessageCreatedEvent).register(priority=999)
@enter_if(_should_handle_chat)
async def block_native_llm_fallback():
    return BLOCK
