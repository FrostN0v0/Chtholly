"""Message-created handler for llm_chat."""

from __future__ import annotations

import re
from typing import Any
import asyncio
from functools import partial
from contextlib import suppress, nullcontext

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
from .steering import current_steering
from .generation import generate_chat_response
from .core.errors import summarize_exception
from .chat_context import (
    build_image_notes,
    render_user_storage,
    serialize_user_turn,
    collect_message_images,
    reply_with_image_views,
    model_supports_image_input,
    build_multimodal_user_content,
    register_message_image_inputs,
)
from .image_inputs import ImageInputs, image_inputs_scope, current_image_inputs
from .reply_record import bind_reply_record
from .steer_inputs import bind_steer_inputs
from .tool_runtime import registered_tool_schemas
from .channel_turns import steer_participant_turn, cancel_active_participant_turns
from .reply_context import collect_reply_context
from .delivery_audit import delivery_audit_scope, current_delivery_audit
from .group_delivery import group_delivery_scope
from .chat_completion import settle_chat_response
from .chat_evaluation import (
    cancel_pending_evaluations,
    schedule_relationship_evaluation,
    schedule_companion_relationship_evaluation,
)
from .forward_context import resolve_merged_forward_messages
from .tools._delivery import send_with_delivery
from .agent_turn_setup import PreparedAgentTurn, prepare_agent_turn
from .agent_attachments import capture_user_input_images
from .companion.runtime import record_companion_chat, interrupt_companion_for_input
from .reaction_feedback import (
    MessageReactionFeedback,
    settle_reaction_update,
    llm_chat_reaction_scope,
)
from .native_image_delivery import native_image_delivery_scope
from .companion.participants import current_companion_participants
from .companion.dialogue_steering import DialogueSteeringState

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
@steer_participant_turn
async def on_chat(session: Session, ctx: Contexts):
    return await handle_chat_turn(session, ctx)


async def handle_chat_turn(
    session: Session, ctx: Contexts, *, quiet: bool = False, turn_config: LLMChatConfig | None = None
):
    """Share native execution while companion admission remains scope-owned."""
    steering = current_steering()
    if not isinstance(steering, DialogueSteeringState):
        interrupt_companion_for_input(session)
    reaction = None if quiet else MessageReactionFeedback(session, _LOGGER.warning)
    inputs = ImageInputs(warn=_LOGGER.warning)
    with (
        group_delivery_scope(session, scheduled_reminder=quiet),
        delivery_audit_scope(session, _LOGGER.warning, scheduled_reminder=quiet),
        llm_chat_reaction_scope(reaction) if reaction is not None else nullcontext(),
        main_model_scope(),
        image_inputs_scope(inputs),
    ):
        try:
            if reaction is not None:
                await reaction.set_stage("processing")
            result = await _run_chat(session, ctx, reaction, turn_config=turn_config)
            if reaction is not None and not reaction.terminal:
                await reaction.finish("failed")
            return result
        except asyncio.CancelledError:
            if reaction is not None:
                await settle_reaction_update(reaction.clear_transient())
            raise
        except Exception:
            if reaction is not None:
                await settle_reaction_update(reaction.finish("failed"))
            raise
        finally:
            close_task = asyncio.create_task(inputs.aclose())
            try:
                await asyncio.shield(close_task)
            except asyncio.CancelledError:
                await close_task
                raise


async def _run_chat(
    session: Session,
    ctx: Contexts,
    reaction: MessageReactionFeedback | None,
    *,
    turn_config: LLMChatConfig | None = None,
):
    active_config = turn_config or config
    model_text = session.elements.extract_plain_text().strip()
    raw_user_text = model_text
    message_images = collect_message_images(session)
    channel_id = session.channel.id
    inputs = current_image_inputs()
    if inputs is None:
        raise RuntimeError("Chat image inputs were not bound")
    reply_to = await bind_reply_record(session, collect_reply_context(session), warn=_LOGGER.warning)
    register_message_image_inputs(session, inputs, reply_to=reply_to)

    try:
        forwarded_messages = await resolve_merged_forward_messages(active_config, session, _LOGGER.warning)
    except Exception as exc:
        _LOGGER.warning(f"merged forward normalization failed: {type(exc).__name__}")
        forwarded_messages = []

    if not model_text and not message_images and not forwarded_messages and reply_to is None:
        if reaction is not None:
            await reaction.finish("failed")
        return BLOCK

    try:
        model_name = pin_main_model(get_model_config(active_config.model, channel_id)).name
    except ModelNotFoundError as exc:
        _LOGGER.warning(f"channel model resolve failed, using global default: {summarize_exception(exc)}")
        model_name = None
    supports_image_input = model_supports_image_input(model_name)
    inputs.supports_image_input = supports_image_input

    try:
        identity = await resolve_chat_identity(session)
    except Exception as exc:
        _LOGGER.warning(f"user identity resolve failed: {summarize_exception(exc)}")
        if reaction is not None:
            await send_with_delivery(session, _CHAT_FAILURE_REPLY, None)
            await reaction.finish("failed")
        return BLOCK
    user_name = identity.display_name
    mentioned_participants = await resolve_mentioned_participants(session)

    current_content: str | list[dict[str, Any]] | None = None
    if supports_image_input:
        current_content, content = await build_multimodal_user_content(
            active_config,
            session,
            user_name,
            model_text,
            _LOGGER.warning,
            forwarded_messages=forwarded_messages,
            mentioned_participants=mentioned_participants,
            reply_to=reply_to,
        )
    else:
        image_notes = await build_image_notes(active_config, session, _LOGGER.warning, forwarded_messages)
        if image_notes:
            model_text = " ".join(part for part in [model_text, *image_notes] if part)
        views = inputs.input_views(session=session)
        reply_to = reply_with_image_views(reply_to, views)
        content = render_user_storage(model_text, forwarded_messages, reply_to=reply_to, input_images=views)
        current_content = serialize_user_turn(
            user_name,
            model_text,
            forwarded_messages,
            mentioned_participants,
            views,
            reply_to=reply_to,
        )

    if not content:
        if reaction is not None:
            await reaction.finish("failed")
        return BLOCK
    input_attachments = await capture_user_input_images(
        session,
        inputs,
        warn=_LOGGER.warning,
    )
    prepared = await prepare_agent_turn(
        active_config,
        session,
        identity,
        model_name=model_name,
        model_text=model_text,
        raw_user_text=raw_user_text,
        content=content,
        current_content=current_content,
        forwarded_messages=forwarded_messages,
        mentioned_participants=mentioned_participants,
        reply_to=reply_to,
        warn=_LOGGER.warning,
        tool_schemas=registered_tool_schemas,
        input_attachments=input_attachments,
        image_inputs=inputs,
        is_operator=await _is_operator(session),
    )
    inputs.commit_input_audit()
    steering = current_steering()
    if steering is not None:
        bind_steer_inputs(steering, prepared, active_config, session, model_name=model_name, warn=_LOGGER.warning)
    if isinstance(steering, DialogueSteeringState):
        from .companion.internal import settle_owned

        try:
            await steering.prepare_first_request(prepared)
        except BaseException as exc:
            await settle_owned(prepared.lifecycle.preserve_and_rollback())
            await settle_owned(
                prepared.lifecycle.finalize_agent_turn(
                    "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
                )
            )
            raise
    status = await run_prepared_agent_turn(
        session, ctx, prepared, model_name=model_name, turn_config=active_config, reaction=reaction
    )
    if isinstance(steering, DialogueSteeringState):
        steering.status = status
    return BLOCK


async def run_prepared_agent_turn(
    session: Session,
    ctx: Contexts | None,
    prepared: PreparedAgentTurn,
    *,
    model_name: str | None,
    reaction: MessageReactionFeedback | None = None,
    turn_config: LLMChatConfig | None = None,
) -> str:
    """Run and finalize a prepared chat or scheduled turn; settlement belongs to the caller."""
    active_config = turn_config or config
    user_id = prepared.agent_access.user_id
    channel_id = session.channel.id

    delivery_audit = current_delivery_audit()
    if delivery_audit is not None:
        delivery_audit.bind(prepared.agent_events)
    chat_messages = prepared.chat_messages
    system = prepared.system
    media_requested = prepared.media_requested
    web_limits = prepared.web_limits
    delivery_state = prepared.delivery_state
    image_inputs = prepared.image_inputs
    turn = prepared.lifecycle
    resolution = prepared.resolution
    agent_events = prepared.agent_events
    agent_access = prepared.agent_access
    turn_status = "failed"
    completion_status: str | None = None

    async def complete(response: object) -> None:
        nonlocal completion_status
        if completion_status is None:
            completion_status = await settle_chat_response(session, prepared, response, reaction)

    try:
        try:
            if reaction is not None:
                await reaction.set_stage("thinking")
            with native_image_delivery_scope(partial(turn.prepare_model_images, session)):
                response = await generate_chat_response(
                    chat_messages,
                    system=system,
                    model=model_name,
                    channel_id=channel_id,
                    ctx=ctx,
                    web_limits=web_limits,
                    delivery_state=delivery_state,
                    image_inputs=image_inputs,
                    media_intent=prepared.media_intent,
                    request_timeout=active_config.model_request_timeout,
                    media_request_timeout=active_config.media_request_timeout,
                    tool_trace=turn.tool_trace,
                    agent_events=agent_events,
                    agent_access=agent_access,
                    resolution=resolution,
                    complete=complete,
                )
        except asyncio.CancelledError:
            turn.capture_tool_events()
            turn_status = "partial" if delivery_state.confirmed_deliveries else "cancelled"
            await turn.preserve_and_rollback()
            if reaction is not None and delivery_state.confirmed_deliveries:
                await settle_reaction_update(reaction.finish("partial"))
            raise
        except Exception as exc:
            turn.capture_tool_events()
            _LOGGER.warning(f"llm generate failed: {summarize_exception(exc)}")
            if delivery_state.delivery_attempts:
                await turn.preserve_and_rollback()
                if reaction is not None:
                    await reaction.finish("partial" if delivery_state.confirmed_deliveries else "failed")
                return "partial" if delivery_state.confirmed_deliveries else "failed"
            if reaction is None:
                await turn.preserve_and_rollback()
                return "failed"
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
            return turn_status

        await complete(response)
        turn_status = completion_status or "failed"
        return turn_status
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
            participants = current_companion_participants()
            if participants is not None:
                agent_events.append(
                    "companion_participation",
                    payload=participants.participation_payload(confirmed=bool(delivery_state.confirmed_deliveries)),
                    model_visible=False,
                )
            await turn.finalize_agent_turn(turn_status)
            if (
                (
                    agent_access.trigger_kind == "message"
                    or (participants is not None and participants.evidence_members())
                )
                and turn_status in {"completed", "silent", "declined"}
                and turn.agent_turn_id is not None
            ):
                if participants is not None:
                    schedule_companion_relationship_evaluation(
                        active_config,
                        turn_id=turn.agent_turn_id,
                        persona_prompt=prepared.persona.prompt,
                        warn=_LOGGER.warning,
                    )
                else:
                    schedule_relationship_evaluation(
                        active_config,
                        turn_id=turn.agent_turn_id,
                        user_id=user_id,
                        channel_id=channel_id,
                        persona_prompt=prepared.persona.prompt,
                        warn=_LOGGER.warning,
                    )
                try:
                    await record_companion_chat(session, prepared)
                except Exception as exc:
                    _LOGGER.warning(f"companion chat observation failed: {type(exc).__name__}")

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
