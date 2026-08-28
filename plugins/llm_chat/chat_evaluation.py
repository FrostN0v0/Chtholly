"""Post-delivery relationship and memory evaluation for llm_chat."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from .config import LLMChatConfig
from .models import Conversation, UserRelation
from .core.eval import apply_deltas
from .core.errors import summarize_exception
from .chat_context import build_eval_conversation
from .persona.store import set_mood, save_relation
from .persona.runner import run_evaluation
from .persona.memory_update import apply_memory_updates
from .persona.memory_context import MemoryContext

WarningSink = Callable[[str], object]


async def update_chat_state_after_delivery(
    config: LLMChatConfig,
    relation: UserRelation,
    memory_context: MemoryContext,
    eval_history: Sequence[Conversation],
    *,
    user_id: str,
    user_name: str,
    channel_id: str,
    user_content: str,
    assistant_reply: str,
    mood: float,
    warn: WarningSink,
) -> None:
    familiarity = min(100.0, relation.familiarity + 1)
    axes = {
        "affection": relation.affection,
        "trust": relation.trust,
        "dependence": relation.dependence,
        "resentment": relation.resentment,
    }
    impression = relation.impression
    counter = relation.eval_counter + 1

    if counter >= config.eval_every_n:
        counter = 0
        conversation = build_eval_conversation(eval_history, user_id, user_name, user_content, assistant_reply)
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
            warn(f"relationship evaluation failed: {summarize_exception(exc)}")
            result = None
        if result is not None:
            axes = apply_deltas(axes, result)
            impression = result.impression
            try:
                await apply_memory_updates(config, user_id, channel_id, result)
            except Exception as exc:
                warn(f"memory update failed: {summarize_exception(exc)}")
            await set_mood(channel_id, mood + result.mood_delta)

    await save_relation(
        user_id,
        channel_id,
        axes=axes,
        impression=impression,
        familiarity=familiarity,
        eval_counter=counter,
    )
