"""Side-effect-free relationship evaluator provider runner."""

from __future__ import annotations

import time
from typing import Protocol, cast
from collections.abc import Mapping, Sequence

import litellm
from entari_plugin_llm.config import get_model_config
from entari_plugin_llm.exception import ModelNotFoundError

from utils.relationship_core import read_emotions

from ..config import LLMChatConfig
from ..core.eval import EvalResult, build_eval_prompt, build_eval_system, parse_eval_response
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
    relationship: Mapping[str, object],
    evaluator_profile_facts: list[ProfileFactData],
    episodes: Sequence[Mapping[str, object]],
    channel_id: str = "$default",
) -> EvalResult | None:
    try:
        conf = get_model_config(config.eval_model or config.model, channel_id)
    except ModelNotFoundError:
        conf = get_model_config(config.eval_model or config.model)
    extra = {
        k: v
        for k, v in conf.extra.items()
        if k not in {"tools", "tool_choice", "response_format", "timeout", "max_retries"}
    }
    expected: list[int] = []
    for episode in episodes:
        turn_id = episode.get("turn_id")
        if isinstance(turn_id, bool) or not isinstance(turn_id, int) or turn_id <= 0:
            raise ValueError("Evaluation episode requires a valid host turn ID")
        expected.append(turn_id)
    response = await litellm.acompletion(
        model=conf.name,
        messages=[
            {
                "role": "system",
                "content": build_eval_system(config.profile_fact_min_confidence, config.memory_min_importance),
            },
            {"role": "user", "content": build_eval_prompt(persona, relationship, evaluator_profile_facts, episodes)},
        ],
        base_url=conf.base_url,
        api_key=conf.api_key,
        temperature=0,
        timeout=config.eval_request_timeout,
        max_retries=0,
        **extra,
    )
    completion = cast(_CompletionLike, response)
    if not completion.choices or not completion.choices[0].message.content:
        return None
    return parse_eval_response(
        completion.choices[0].message.content,
        expected_turn_ids=expected,
        previous_emotions=read_emotions(relationship.get("emotions", [])),
        now=time.time(),
        current_impression=str(relationship.get("impression", "")),
        min_memory_importance=config.memory_min_importance,
        min_profile_confidence=config.profile_fact_min_confidence,
    )
