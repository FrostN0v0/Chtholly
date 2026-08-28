"""Prepare one claimed chat turn and its AgentEvent session context."""

from __future__ import annotations

from typing import Any, cast
from datetime import datetime
from dataclasses import dataclass
from collections.abc import Mapping, Callable, Sequence

from arclet.entari import Session

from .config import LLMChatConfig
from .models import Conversation, UserRelation
from .identity import ChatIdentity
from .core.types import ChatMessage
from .web.policy import WebAccessLimits, normalize_web_access_limits
from .agent_events import persist_agent_events
from .chat_context import build_chat_messages, serialize_user_turn, requests_recent_channel_context
from .core.compose import energy_at, compose_persona_prompt
from .core.forward import ForwardedMessage
from .agent_context import AgentAccessContext
from .core.delivery import DeliveryState, normalize_delivery_limits
from .persona.store import get_mood, get_relation, load_history, append_message, delete_message
from .channel_images import ChannelImageReferences
from .turn_lifecycle import ActiveChatTurn
from .context_builder import (
    requests_context_pin,
    requests_tool_payload,
    requests_fresh_context,
    select_session_context,
    render_session_baseline,
    requests_archived_context,
    build_baseline_fingerprint,
)
from .core.engagement import (
    EngagementBudget,
    EngagementSignals,
    EngagementDecision,
    decide_engagement,
    engagement_budget,
    apply_engagement_budget,
    engagement_event_payload,
    engagement_prompt_context,
)
from .session_handoff import generate_session_handoff
from .session_manager import (
    start_turn,
    finish_turn,
    rollover_session,
    load_scope_anchors,
    get_or_create_scope,
    ensure_active_session,
    resolve_scope_identity,
)
from .core.agent_trace import AgentTurnRecorder
from .engagement_state import collect_engagement_signals
from .core.media_delivery import latest_user_requests_media, latest_user_requests_image_generation
from .core.self_reference import append_self_reference_image
from .persona.memory_context import MemoryContext, load_memory_context

WarningSink = Callable[[str], None]


@dataclass(slots=True)
class PreparedAgentTurn:
    relation: UserRelation
    mood: float
    memory_context: MemoryContext
    eval_history: list[Conversation]
    chat_messages: list[ChatMessage]
    system: str
    media_requested: bool
    web_limits: WebAccessLimits
    delivery_state: DeliveryState
    channel_image_references: ChannelImageReferences
    lifecycle: ActiveChatTurn
    agent_events: AgentTurnRecorder
    agent_access: AgentAccessContext
    engagement: EngagementDecision
    engagement_budget: EngagementBudget
    engagement_signals: EngagementSignals


async def prepare_agent_turn(
    config: LLMChatConfig,
    session: Session,
    identity: ChatIdentity,
    *,
    model_name: str | None,
    supports_image_input: bool,
    model_text: str,
    content: str,
    current_content: str | list[dict[str, Any]] | None,
    forwarded_messages: Sequence[ForwardedMessage],
    warn: WarningSink,
    tool_schemas: Sequence[Mapping[str, object]],
    requires_media_reply: bool = False,
    is_operator: bool = False,
) -> PreparedAgentTurn:
    channel_id = session.channel.id
    user_id = identity.user_id
    user_name = identity.display_name
    relation = await get_relation(user_id, channel_id)
    mood = await get_mood(channel_id)
    memory_context = await load_memory_context(config, user_id, channel_id, content)
    pending_eval = relation.eval_counter + 1 >= config.eval_every_n
    eval_history = (
        await load_history(channel_id, config.eval_context_window)
        if pending_eval and config.eval_context_window > 0
        else []
    )

    current_messages = cast(
        list[ChatMessage],
        build_chat_messages([], user_name, model_text, current_content, forwarded_messages),
    )
    self_reference_attached = False
    if supports_image_input and latest_user_requests_image_generation(current_messages):
        self_reference_attached = append_self_reference_image(
            current_messages,
            config.self_reference_image,
            warn,
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
    signals = await collect_engagement_signals(
        user_id=user_id,
        channel_id=channel_id,
        relation=relation,
        user_mood=mood,
        energy=energy_at(datetime.now().hour),
        text=model_text,
        is_command=False,
        is_private=not str(getattr(getattr(session, "guild", None), "id", "") or ""),
        is_operator=is_operator,
        requires_media_reply=requires_media_reply,
    )
    engagement = decide_engagement(signals)
    budget = engagement_budget(engagement.level, delivery_limits)
    delivery_limits = apply_engagement_budget(delivery_limits, budget)
    delivery_state = DeliveryState(limits=delivery_limits)
    baseline = build_baseline_fingerprint(
        model_name=model_name or "default",
        persona=config.persona,
        tool_schemas=tool_schemas,
    )
    scope = await get_or_create_scope(await resolve_scope_identity(session))
    context_session, rollover_reason = await ensure_active_session(
        scope,
        baseline,
        idle_minutes=config.session_idle_minutes,
        max_turns=config.session_max_turns,
    )
    if rollover_reason is not None:
        handoff = await _handoff(config, context_session, channel_id)
        context_session = await rollover_session(
            scope,
            context_session,
            baseline,
            reason=rollover_reason,
            handoff_json=handoff,
        )

    anchors = await load_scope_anchors(scope.id)

    def compose_system() -> str:
        return compose_persona_prompt(
            config.persona,
            mood,
            energy_at(datetime.now().hour),
            affection=relation.affection,
            trust=relation.trust,
            dependence=relation.dependence,
            resentment=relation.resentment,
            familiarity=relation.familiarity,
            impression=relation.impression,
            profile=memory_context.chat_profile,
            relevant_memories=memory_context.relevant_memories,
            agent_session=render_session_baseline(context_session, anchors),
            user_name=user_name,
            current_participant_ref=identity.participant_ref,
            self_reference_attached=self_reference_attached,
            web_search_limit=web_limits.search_limit,
            web_page_limit=web_limits.read_limit,
            web_total_limit=web_limits.total_limit,
            delivery_limits=delivery_limits,
            engagement=engagement_prompt_context(engagement, budget),
        )

    system = compose_system()
    fresh_context = requests_fresh_context(model_text) or requests_recent_channel_context(model_text)
    selection = await _select_context(
        config,
        context_session,
        current_messages[-1],
        system=system,
        model_name=model_name,
        fresh_context=fresh_context,
    )
    if selection.rollover_required:
        handoff = await _handoff(config, context_session, channel_id)
        context_session = await rollover_session(
            scope,
            context_session,
            baseline,
            reason="context_budget",
            handoff_json=handoff,
        )
        system = compose_system()
        selection = await _select_context(
            config,
            context_session,
            current_messages[-1],
            system=system,
            model_name=model_name,
            fresh_context=fresh_context,
        )

    user_message_id = await append_message(channel_id, user_id, user_name, "user", content)
    event_message = getattr(session.event, "message", None)
    try:
        agent_turn = await start_turn(
            context_session,
            trigger_message_id=str(getattr(event_message, "id", "") or ""),
            user_id=user_id,
            user_name=user_name,
            conversation_user_id=user_message_id,
            fresh_context=fresh_context,
        )
    except Exception:
        await delete_message(user_message_id)
        raise

    agent_events = AgentTurnRecorder()
    agent_events.record_user_input(
        serialize_user_turn(user_name, content),
        user_name=user_name,
        fresh_context=fresh_context,
    )
    agent_events.record_persona_state(
        {
            "relation": {
                "affection": round(relation.affection, 3),
                "trust": round(relation.trust, 3),
                "dependence": round(relation.dependence, 3),
                "resentment": round(relation.resentment, 3),
                "familiarity": round(relation.familiarity, 3),
                "impression": relation.impression,
                "eval_counter": relation.eval_counter,
            },
            "state": {
                "mood": round(mood, 3),
                "energy": round(energy_at(datetime.now().hour), 3),
                "pending_eval": pending_eval,
            },
            "memory": memory_context.retrieval,
            "prompt_profile": memory_context.chat_profile,
            "prompt_memories": list(memory_context.relevant_memories),
            "budgets": {
                "max_input_tokens": config.max_input_tokens,
                "output_reserve_tokens": config.output_reserve_tokens,
                "estimated_tokens": selection.estimated_tokens,
                "full_session_tokens": selection.full_session_tokens,
            },
        }
    )
    agent_events.append(
        "engagement_decision",
        payload=engagement_event_payload(engagement, budget, signals),
        status=engagement.level,
        model_visible=False,
    )
    agent_events.append(
        "context_selection",
        payload={
            "estimated_tokens": selection.estimated_tokens,
            "full_session_tokens": selection.full_session_tokens,
            "included_turn_refs": list(selection.included_turn_refs),
            "excluded_turn_refs": list(selection.excluded_turn_refs),
        },
        model_visible=False,
    )
    await _flush_started_events(agent_turn.id, agent_events, warn)
    lifecycle = ActiveChatTurn(
        channel_id=channel_id,
        user_message_id=user_message_id,
        delivery_state=delivery_state,
        append_history=append_message,
        delete_history=delete_message,
        warn=warn,
        persist_agent_event_rows=persist_agent_events,
        finish_agent_turn_row=finish_turn,
        agent_turn_id=agent_turn.id,
        agent_events=agent_events,
    )
    return PreparedAgentTurn(
        relation=relation,
        mood=mood,
        memory_context=memory_context,
        eval_history=eval_history,
        chat_messages=selection.messages,
        system=system,
        media_requested=latest_user_requests_media(selection.messages),
        web_limits=web_limits,
        delivery_state=delivery_state,
        channel_image_references=ChannelImageReferences(),
        lifecycle=lifecycle,
        agent_events=agent_events,
        engagement=engagement,
        engagement_budget=budget,
        engagement_signals=signals,
        agent_access=AgentAccessContext(
            scope_id=scope.id,
            session_id=context_session.id,
            turn_id=agent_turn.id,
            user_id=user_id,
            allow_archived_sessions=requests_archived_context(model_text),
            allow_payload_delivery=requests_tool_payload(model_text),
            allow_context_pin=requests_context_pin(model_text),
        ),
    )


async def _flush_started_events(
    turn_id: int,
    recorder: AgentTurnRecorder,
    warn: WarningSink,
) -> None:
    """Persist turn-start evidence immediately so running turns are inspectable."""

    pending = recorder.pending_events()
    if not pending:
        return
    try:
        await persist_agent_events(turn_id, pending)
    except Exception as exc:
        warn(f"agent turn start persistence failed: {type(exc).__name__}")
        return
    recorder.mark_flushed(len(pending))


async def _handoff(config: LLMChatConfig, context_session, channel_id: str) -> str:
    return await generate_session_handoff(
        context_session,
        model_name=config.model,
        channel_id=channel_id,
        timeout=config.session_handoff_timeout,
        source_max_chars=config.session_handoff_source_max_chars,
        output_max_chars=config.session_handoff_max_chars,
    )


async def _select_context(
    config: LLMChatConfig,
    context_session,
    current_message: ChatMessage,
    *,
    system: str,
    model_name: str | None,
    fresh_context: bool,
):
    return await select_session_context(
        context_session,
        system=system,
        current_message=current_message,
        model_name=model_name,
        max_input_tokens=max(2048, config.max_input_tokens),
        output_reserve_tokens=max(0, config.output_reserve_tokens),
        rollover_ratio=config.context_rollover_ratio,
        minimum_recent_turns=max(0, config.context_min_recent_turns),
        inline_event_chars=max(256, config.context_inline_event_chars),
        fresh_context=fresh_context,
    )
