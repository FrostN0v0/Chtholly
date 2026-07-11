"""Chat generation orchestration with one tool-free exhaustion fallback."""

from __future__ import annotations

from typing import cast

import litellm
from entari_plugin_llm import llm  # entari: plugin
from arclet.letoderea.context import Contexts
from entari_plugin_llm._types import Message
from entari_plugin_llm.config import get_model_config

from .web_access import WebAccessLimits, llm_chat_web_access_scope

_TOOL_LOOP_EXHAUSTED = "LLM completion did not return a response"
_FINALIZATION_FAILED = "LLM finalization did not return a response"
_FINALIZATION_SUFFIX = (
    "工具调用轮次已结束。不得再调用任何工具；请仅依据本轮已返回的工具结果和已有对话直接给出最终答复。"
    "若证据不足，明确说明未核实或无法确认的部分，不得编造搜索、读取或执行结果。"
    "若已有媒体工具成功完成且无需补充文字，只返回 [END_OF_RESPONSE]。"
)


async def generate_chat_response(
    messages: list[Message],
    *,
    system: str,
    model: str | None,
    channel_id: str,
    ctx: Contexts,
    web_limits: WebAccessLimits,
) -> litellm.ModelResponse:
    """Generate normally, then finalize once without tools after exact exhaustion."""

    try:
        with llm_chat_web_access_scope(web_limits):
            return cast(
                litellm.ModelResponse,
                await llm.generate(messages, system=system, model=model, ctx=ctx),
            )
    except RuntimeError as exc:
        if str(exc) != _TOOL_LOOP_EXHAUSTED:
            raise

    return await _finalize_without_tools(
        messages,
        system=system,
        model=model,
        channel_id=channel_id,
    )


async def _finalize_without_tools(
    messages: list[Message],
    *,
    system: str,
    model: str | None,
    channel_id: str,
) -> litellm.ModelResponse:
    conf = get_model_config(model, channel_id)
    extra = {key: value for key, value in conf.extra.items() if key not in {"tools", "tool_choice"}}
    response = await litellm.acompletion(
        model=conf.name,
        messages=[
            {"role": "system", "content": f"{system}\n\n{_FINALIZATION_SUFFIX}"},
            *messages,
        ],
        base_url=conf.base_url,
        api_key=conf.api_key,
        **extra,
    )
    completion = cast(litellm.ModelResponse, response)
    if not completion.choices:
        raise RuntimeError(_FINALIZATION_FAILED)
    content = completion.choices[0].message.content
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError(_FINALIZATION_FAILED)
    return completion
