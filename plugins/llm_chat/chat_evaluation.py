"""Post-delivery relationship and memory evaluation for llm_chat."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence

from .config import LLMChatConfig
from .models import Conversation
from .core.errors import summarize_exception
from .chat_context import build_eval_conversation
from .persona.store import (
    adjust_mood,
    get_relation,
    load_history,
    apply_relationship_evaluation,
    claim_relationship_evaluation,
    restore_relationship_evaluation,
)
from .persona.runner import run_evaluation
from .persona.memory_update import apply_memory_updates
from .persona.memory_context import MemoryContext

WarningSink = Callable[[str], object]

_PENDING_EVALUATIONS: set[asyncio.Task[None]] = set()


def cancel_pending_evaluations() -> None:
    """Cancel evaluator work during plugin unload or hot reload."""

    for task in tuple(_PENDING_EVALUATIONS):
        task.cancel()


def schedule_chat_state_after_delivery(
    config: LLMChatConfig,
    memory_context: MemoryContext,
    eval_history: Sequence[Conversation],
    *,
    user_id: str,
    user_name: str,
    channel_id: str,
    user_content: str,
    assistant_reply: str,
    warn: WarningSink,
) -> None:
    """Schedule evaluator work without delaying or coupling it to the reply turn."""

    async def run() -> None:
        try:
            await update_chat_state_after_delivery(
                config,
                memory_context,
                eval_history,
                user_id=user_id,
                user_name=user_name,
                channel_id=channel_id,
                user_content=user_content,
                assistant_reply=assistant_reply,
                warn=warn,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            warn(f"relationship state update failed: {summarize_exception(exc)}")

    task = asyncio.create_task(run(), name=f"llm-chat-eval:{channel_id}:{user_id}")
    _PENDING_EVALUATIONS.add(task)
    task.add_done_callback(_PENDING_EVALUATIONS.discard)


async def update_chat_state_after_delivery(
    config: LLMChatConfig,
    memory_context: MemoryContext,
    eval_history: Sequence[Conversation],
    *,
    user_id: str,
    user_name: str,
    channel_id: str,
    user_content: str,
    assistant_reply: str,
    warn: WarningSink,
) -> None:
    claimed = await claim_relationship_evaluation(user_id, channel_id, config.eval_every_n)
    if not claimed:
        return

    relation = await get_relation(user_id, channel_id)
    axes = {
        "affection": relation.affection,
        "trust": relation.trust,
        "dependence": relation.dependence,
        "resentment": relation.resentment,
    }
    history = list(eval_history)
    if not history and config.eval_context_window > 0:
        history = await load_history(channel_id, config.eval_context_window)
    conversation = build_eval_conversation(history, user_id, user_name, user_content, assistant_reply)
    try:
        result = await run_evaluation(
            config,
            config.persona,
            axes,
            relation.impression,
            memory_context.evaluator_profile_facts,
            conversation,
            user_name,
            channel_id,
        )
    except asyncio.CancelledError:
        restore_task = asyncio.create_task(restore_relationship_evaluation(user_id, channel_id, config.eval_every_n))
        try:
            await asyncio.shield(restore_task)
        except asyncio.CancelledError:
            await restore_task
        raise
    except Exception as exc:
        await restore_relationship_evaluation(user_id, channel_id, config.eval_every_n)
        warn(f"relationship evaluation failed: {summarize_exception(exc)}")
        return
    if result is None:
        await restore_relationship_evaluation(user_id, channel_id, config.eval_every_n)
        warn("relationship evaluation failed: invalid response")
        return

    await apply_relationship_evaluation(
        user_id,
        channel_id,
        deltas=result.deltas,
        impression=result.impression,
    )
    try:
        await apply_memory_updates(config, user_id, channel_id, result)
    except Exception as exc:
        warn(f"memory update failed: {summarize_exception(exc)}")
    await adjust_mood(channel_id, result.mood_delta)
