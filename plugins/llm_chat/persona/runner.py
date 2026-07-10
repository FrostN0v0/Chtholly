"""Evaluator LLM call: bypasses the llm plugin's tool loop.

llm.generate() hardcodes tools + tool_choice into every payload, which would
let the evaluator trigger side-effect tools. We call litellm directly with the
plugin's resolved model config instead (same credentials, no tools).
"""

from typing import Protocol, cast

import litellm
from entari_plugin_llm.config import get_model_config

from ..config import LLMChatConfig
from ..core.eval import (
    EvalResult,
    EvalConversation,
    build_eval_prompt,
    build_eval_system,
    parse_eval_response,
)
from ..core.memory_policy import ProfileFactData


class _MessageLike(Protocol):
    content: str | None


class _ChoiceLike(Protocol):
    message: _MessageLike


class _CompletionLike(Protocol):
    choices: list[_ChoiceLike]


async def run_evaluation(
    config: LLMChatConfig,
    persona: str,
    axes: dict[str, float],
    impression: str,
    evaluator_profile_facts: list[ProfileFactData],
    conversation: EvalConversation,
    user_name: str = "",
    channel_id: str = "$default",
) -> EvalResult | None:
    """Run the relationship evaluator; returns None when parsing fails."""
    try:
        conf = get_model_config(config.eval_model or config.model, channel_id)
    except Exception:
        # Stale channel default: fall back to the "$default" scope resolution.
        conf = get_model_config(config.eval_model or config.model)
    extra = {key: value for key, value in conf.extra.items() if key not in {"tools", "tool_choice"}}
    response = await litellm.acompletion(
        model=conf.name,
        messages=[
            {
                "role": "system",
                "content": build_eval_system(
                    config.profile_fact_min_confidence,
                    config.memory_min_importance,
                ),
            },
            {
                "role": "user",
                "content": build_eval_prompt(
                    persona,
                    axes,
                    impression,
                    evaluator_profile_facts,
                    conversation,
                    user_name,
                ),
            },
        ],
        base_url=conf.base_url,
        api_key=conf.api_key,
        temperature=0,
        response_format={"type": "json_object"},
        **extra,
    )
    completion = cast(_CompletionLike, response)
    content = completion.choices[0].message.content
    if not content:
        return None
    return parse_eval_response(
        content,
        current_impression=impression,
        min_memory_importance=config.memory_min_importance,
        min_profile_confidence=config.profile_fact_min_confidence,
    )
