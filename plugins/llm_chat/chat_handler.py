"""Message-created handler for llm_chat."""

from __future__ import annotations

from typing import Any, cast
from datetime import datetime

from arclet.entari import At, Session, MessageCreatedEvent, plugin_config
from arclet.letoderea import BLOCK, enter_if
from arclet.entari.logger import log
from arclet.letoderea.context import Contexts
from entari_plugin_llm._types import Message
from entari_plugin_llm.config import get_model_config
from arclet.entari.plugin.model import Plugin

from .config import LLMChatConfig
from .core.eval import apply_deltas
from .core.media import strip_internal_media_records
from .generation import generate_chat_response
from .web_access import normalize_web_access_limits
from .chat_context import (
    build_image_notes,
    build_chat_messages,
    build_eval_conversation,
    model_supports_image_input,
    build_multimodal_user_content,
)
from .core.compose import energy_at, compose_persona_prompt
from .persona.store import get_mood, set_mood, get_relation, load_history, save_relation, append_message
from .persona.runner import run_evaluation
from .persona.memory_update import apply_memory_updates
from .persona.memory_context import load_memory_context

_LOGGER = log.wrapper("[llm_chat]")


async def _addressed_to_me(session: Session, is_reply_me: bool = False, is_notice_me: bool = False) -> bool:
    """Accept to-me replies/notices plus At(bot) at any position."""
    if is_reply_me or is_notice_me:
        return True
    self_id = session.account.self_id
    return any(at.id == self_id for at in session.elements.select(At) if at.id)


config = plugin_config(LLMChatConfig)
plug = Plugin.current()


@plug.dispatch(MessageCreatedEvent).register(priority=900)
@enter_if(_addressed_to_me)
async def on_chat(session: Session, ctx: Contexts):
    content = session.elements.extract_plain_text().strip()

    try:
        model_name = get_model_config(config.model, session.channel.id).name
    except Exception as exc:
        _LOGGER.warning(f"channel model resolve failed, using global default: {exc!r}")
        model_name = None

    current_content: str | list[dict[str, Any]] | None = None
    if model_supports_image_input(model_name):
        current_content, content = await build_multimodal_user_content(
            config,
            session,
            (session.member.nick if session.member else None) or session.user.name or session.user.id,
            content,
            _LOGGER.warning,
        )
    else:
        image_notes = await build_image_notes(config, session, _LOGGER.warning)
        if image_notes:
            content = " ".join(part for part in [content, *image_notes] if part)

    if not content:
        return BLOCK

    channel_id = session.channel.id
    user_id = session.user.id
    user_name = (session.member.nick if session.member else None) or session.user.name or user_id

    rel = await get_relation(user_id, channel_id)
    mood = await get_mood(channel_id)
    energy = energy_at(datetime.now().hour)
    memory_context = await load_memory_context(config, user_id, channel_id, content)
    history = await load_history(channel_id, config.context_window)
    messages = build_chat_messages(history, user_name, content, current_content)
    web_limits = normalize_web_access_limits(
        config.web_search_max_calls_per_generation,
        config.web_page_max_calls_per_generation,
        config.web_total_max_calls_per_generation,
    )
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
    )

    await append_message(channel_id, user_id, user_name, "user", content)

    try:
        response = await generate_chat_response(
            cast(list[Message], messages),
            system=system,
            model=model_name,
            channel_id=channel_id,
            ctx=ctx,
            web_limits=web_limits,
        )
    except Exception as exc:
        _LOGGER.warning(f"llm generate failed: {exc!r}")
        return BLOCK

    raw_reply = cast(str | None, response.choices[0].message.content) or ""
    reply = strip_internal_media_records(raw_reply)
    if reply != raw_reply:
        _LOGGER.warning("stripped reserved media history marker from model reply")
    if reply and reply != "[END_OF_RESPONSE]":
        await session.send(reply)
        await append_message(channel_id, "", "bot", "assistant", reply)

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
        conversation = build_eval_conversation(recent, user_id, user_name, content, reply)
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
            _LOGGER.warning(f"relationship evaluation failed: {exc!r}")
            result = None
        if result is not None:
            axes = apply_deltas(axes, result)
            impression = result.impression
            try:
                await apply_memory_updates(config, user_id, channel_id, result)
            except Exception as exc:
                _LOGGER.warning(f"memory update failed: {exc!r}")
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
