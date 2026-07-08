"""Evaluator LLM call: bypasses the llm plugin's tool loop.

llm.generate() hardcodes tools + tool_choice into every payload, which would
let the evaluator trigger side-effect tools. We call litellm directly with the
plugin's resolved model config instead (same credentials, no tools).
"""

import litellm
from entari_plugin_llm.config import get_model_config

from .eval import EVAL_SYSTEM, EvalResult, build_eval_prompt, parse_eval_response
from ..config import LLMChatConfig


async def run_evaluation(
    config: LLMChatConfig,
    persona: str,
    axes: dict[str, float],
    impression: str,
    profile_facts: list[str],
    transcript: list[str],
    user_name: str = "",
    channel_id: str = "$default",
) -> EvalResult | None:
    """Run the relationship evaluator; returns None when parsing fails."""
    try:
        conf = get_model_config(config.eval_model or config.model, channel_id)
    except Exception:
        # Stale channel default: fall back to the "$default" scope resolution.
        conf = get_model_config(config.eval_model or config.model)
    response = await litellm.acompletion(
        model=conf.name,
        messages=[
            {"role": "system", "content": EVAL_SYSTEM},
            {
                "role": "user",
                "content": build_eval_prompt(persona, axes, impression, profile_facts, transcript, user_name),
            },
        ],
        base_url=conf.base_url,
        api_key=conf.api_key,
        temperature=0,
        response_format={"type": "json_object"},
        **conf.extra,
    )
    content = response.choices[0].message.content  # type: ignore[union-attr]
    if not content:
        return None
    return parse_eval_response(
        content,
        current_impression=impression,
        min_profile_confidence=config.profile_fact_min_confidence,
    )
