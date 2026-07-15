"""Message-created handler for llm_chat."""

from __future__ import annotations

from typing import Any, cast
import asyncio
from datetime import datetime

from arclet.entari import At, Session, MessageCreatedEvent, plugin_config
from arclet.letoderea import BLOCK, enter_if
from arclet.entari.logger import log
from arclet.letoderea.context import Contexts
from entari_plugin_llm.config import get_model_config
from arclet.entari.plugin.model import Plugin
from entari_plugin_llm.exception import ModelNotFoundError

from .config import LLMChatConfig
from .core.eval import apply_deltas
from .core.media import strip_internal_media_records
from .core.types import ChatMessage
from .generation import generate_chat_response
from .web_access import normalize_web_access_limits
from .core.errors import summarize_exception
from .chat_context import (
    build_image_notes,
    build_chat_messages,
    build_eval_conversation,
    model_supports_image_input,
    build_multimodal_user_content,
)
from .core.compose import energy_at, compose_persona_prompt
from .core.forward import render_forwarded_storage
from .core.delivery import (
    DeliveryError,
    DeliveryState,
    wait_for_delivery,
    reserve_final_text,
    mark_delivery_attempt,
    mark_delivery_success,
    render_delivered_text,
    normalize_delivery_limits,
)
from .persona.store import (
    get_mood,
    set_mood,
    get_relation,
    load_history,
    save_relation,
    append_message,
    delete_message,
)
from .persona.runner import run_evaluation
from .forward_context import has_direct_merged_forward, resolve_merged_forward_messages
from .persona.memory_update import apply_memory_updates
from .persona.memory_context import load_memory_context

_LOGGER = log.wrapper("[llm_chat]")


async def _addressed_to_me(session: Session, is_reply_me: bool = False, is_notice_me: bool = False) -> bool:
    """Accept explicit mentions/replies and optionally direct merged forwards."""
    if is_reply_me or is_notice_me:
        return True
    self_id = session.account.self_id
    if any(at.id == self_id for at in session.elements.select(At) if at.id):
        return True
    return config.merged_forward_auto_reply and has_direct_merged_forward(session)


config = plugin_config(LLMChatConfig)
plug = Plugin.current()


@plug.dispatch(MessageCreatedEvent).register(priority=900)
@enter_if(_addressed_to_me)
async def on_chat(session: Session, ctx: Contexts):
    model_text = session.elements.extract_plain_text().strip()
    channel_id = session.channel.id
    user_id = session.user.id
    user_name = (session.member.nick if session.member else None) or session.user.name or user_id

    try:
        forwarded_messages = await resolve_merged_forward_messages(config, session, _LOGGER.warning)
    except Exception as exc:
        _LOGGER.warning(f"merged forward normalization failed: {type(exc).__name__}")
        forwarded_messages = []

    try:
        model_name = get_model_config(config.model, channel_id).name
    except ModelNotFoundError as exc:
        _LOGGER.warning(f"channel model resolve failed, using global default: {summarize_exception(exc)}")
        model_name = None

    current_content: str | list[dict[str, Any]] | None = None
    if model_supports_image_input(model_name):
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

    rel = await get_relation(user_id, channel_id)
    mood = await get_mood(channel_id)
    energy = energy_at(datetime.now().hour)
    memory_context = await load_memory_context(config, user_id, channel_id, content)
    history = await load_history(channel_id, config.context_window)
    messages = build_chat_messages(
        history,
        user_name,
        model_text,
        current_content,
        forwarded_messages,
    )
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
        user_name=user_name,
        web_search_limit=web_limits.search_limit,
        web_page_limit=web_limits.read_limit,
        web_total_limit=web_limits.total_limit,
        delivery_limits=delivery_limits,
    )

    user_message_id = await append_message(channel_id, user_id, user_name, "user", content)

    assistant_persist_attempted = False

    async def persist_delivered_text(*, preserve_original: bool = False) -> str:
        nonlocal assistant_persist_attempted
        delivered_text = render_delivered_text(delivery_state)
        if assistant_persist_attempted or not delivered_text:
            return delivered_text
        assistant_persist_attempted = True
        try:
            await append_message(channel_id, "", "bot", "assistant", delivered_text)
        except asyncio.CancelledError:
            _LOGGER.warning("assistant delivery persistence cancelled")
            if not preserve_original:
                raise
        except Exception as exc:
            _LOGGER.warning(f"assistant delivery persistence failed: {type(exc).__name__}")
        return delivered_text

    async def rollback_unstarted_turn() -> None:
        if delivery_state.delivery_attempts:
            return
        try:
            await delete_message(user_message_id)
        except asyncio.CancelledError:
            _LOGGER.warning("user turn rollback cancelled")
        except Exception as exc:
            _LOGGER.warning(f"user turn rollback failed: {type(exc).__name__}")

    try:
        response = await generate_chat_response(
            cast(list[ChatMessage], messages),
            system=system,
            model=model_name,
            channel_id=channel_id,
            ctx=ctx,
            web_limits=web_limits,
            delivery_state=delivery_state,
            request_timeout=config.model_request_timeout,
        )
    except asyncio.CancelledError:
        await persist_delivered_text(preserve_original=True)
        await rollback_unstarted_turn()
        raise
    except Exception as exc:
        await persist_delivered_text(preserve_original=True)
        await rollback_unstarted_turn()
        _LOGGER.warning(f"llm generate failed: {summarize_exception(exc)}")
        return BLOCK

    raw_reply = cast(str | None, response.choices[0].message.content) or ""
    stripped_raw_reply = raw_reply.strip()
    reply = strip_internal_media_records(raw_reply).strip()
    if reply != stripped_raw_reply:
        _LOGGER.warning("stripped reserved media history marker from model reply")
    if (not reply or reply == "[END_OF_RESPONSE]") and delivery_state.confirmed_deliveries == 0:
        await rollback_unstarted_turn()
        _LOGGER.warning("model reply produced no confirmed delivery")
        return BLOCK
    if reply and reply != "[END_OF_RESPONSE]":
        if delivery_state.mode is not None:
            try:
                reply = reserve_final_text(delivery_state, reply)
            except DeliveryError:
                _LOGGER.warning("suppressed final supplement outside delivery budget")
                reply = ""
        if not reply and delivery_state.confirmed_deliveries == 0:
            await rollback_unstarted_turn()
            _LOGGER.warning("final reply was suppressed without confirmed delivery")
            return BLOCK
        if reply:
            try:
                await wait_for_delivery(delivery_state)
            except asyncio.CancelledError:
                await persist_delivered_text(preserve_original=True)
                await rollback_unstarted_turn()
                raise
            except Exception:
                await persist_delivered_text(preserve_original=True)
                await rollback_unstarted_turn()
                raise
            try:
                await session.send(reply)
            except asyncio.CancelledError:
                mark_delivery_attempt(delivery_state)
                await persist_delivered_text(preserve_original=True)
                raise
            except Exception:
                mark_delivery_attempt(delivery_state)
                await persist_delivered_text(preserve_original=True)
                raise
            mark_delivery_success(delivery_state, [reply])

    assistant_reply = await persist_delivered_text()
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
@enter_if(_addressed_to_me)
async def block_native_llm_fallback():
    return BLOCK
