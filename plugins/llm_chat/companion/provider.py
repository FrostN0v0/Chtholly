"""One audited, bounded model request for a host-owned internal reflection."""

from __future__ import annotations

from typing import Protocol, cast
from collections.abc import Mapping

import litellm
from entari_plugin_llm.config import get_model_config
from entari_plugin_llm.exception import ModelNotFoundError

from utils.companion_core import CompanionLimits, ReflectionProposal, parse_reflection

from ..config import LLMChatConfig
from ..core.agent_trace import AgentTurnRecorder
from ..model_audit_runtime import model_audit_scope, capture_completion


class _Message(Protocol):
    content: str | None


class _Choice(Protocol):
    message: _Message


class _Response(Protocol):
    choices: list[_Choice]


def reflection_model(config: LLMChatConfig, channel_id: str):
    try:
        return get_model_config(config.companion_model or config.model, channel_id)
    except ModelNotFoundError:
        return get_model_config(config.companion_model or config.model)


async def request_reflection(
    model,
    *,
    system: str,
    prompt: str,
    limits: CompanionLimits,
    member_turn_ids: Mapping[str, tuple[int, ...]],
    recorder: AgentTurnRecorder,
    allow_channel: bool = False,
) -> ReflectionProposal:
    if len(system) + len(prompt) > limits.input_chars:
        raise ValueError("Companion reflection input exceeds the configured character budget")
    excluded = {
        "tools",
        "tool_choice",
        "response_format",
        "timeout",
        "max_retries",
        "stream",
        "max_tokens",
        "max_completion_tokens",
        "temperature",
        "parallel_tool_calls",
        "n",
    }
    extra = {key: value for key, value in model.extra.items() if key not in excluded}
    with model_audit_scope(recorder):
        response = await capture_completion(
            litellm.acompletion,
            model=model.name,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            base_url=model.base_url,
            api_key=model.api_key,
            timeout=limits.request_timeout,
            max_retries=0,
            max_tokens=limits.output_tokens,
            **extra,
        )
    result = cast(_Response, response)
    if not result.choices or not result.choices[0].message.content:
        raise ValueError("Companion reflection returned no structured decision")
    return parse_reflection(
        result.choices[0].message.content,
        member_refs=set(member_turn_ids),
        turn_ids={turn_id for values in member_turn_ids.values() for turn_id in values},
        member_turn_ids=member_turn_ids,
        allow_channel=allow_channel,
    )
