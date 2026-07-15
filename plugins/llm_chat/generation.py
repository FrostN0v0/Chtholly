"""Chat generation orchestration with bounded tool-free recovery."""

from __future__ import annotations

from typing import cast

import litellm
from entari_plugin_llm import llm  # entari: plugin
from arclet.entari.logger import log
from arclet.letoderea.context import Contexts
from entari_plugin_llm.config import get_model_config

from .core.media import strip_internal_media_records
from .core.types import ChatMessage
from .web_access import WebAccessLimits, llm_chat_web_access_scope
from .core.delivery import DeliveryState, llm_chat_delivery_scope

_TOOL_LOOP_EXHAUSTED = "LLM completion did not return a response"
_FINALIZATION_FAILED = "LLM finalization did not return a response"
_END_OF_RESPONSE = "[END_OF_RESPONSE]"
_FINALIZATION_SUFFIX = (
    "工具调用轮次已结束。不得再调用任何工具；请仅依据本轮已返回的工具结果和已有对话直接给出最终答复。"
    "若证据不足，明确说明未核实或无法确认的部分，不得编造搜索、读取或执行结果。"
    "若已有任意发送工具成功，不得复述已发送内容；若工具错误包含 merged forward fallback confirmed 与 "
    "do not repeat the confirmed prefix，也不得复述其中已确认的前缀。"
    "仅补充尚未发送且有依据的新信息，否则只返回 [END_OF_RESPONSE]。"
)
_VISIBLE_RETRY_SUFFIX = (
    "上一条候选回复不可发送：它为空、只包含结束控制标记，或复述了历史中的媒体发送记录。"
    "本轮没有发生任何发送尝试。不得再调用工具，不得输出媒体发送记录或 [END_OF_RESPONSE]，"
    "也不得声称已经发送媒体；请直接给出一条自然、可见且符合当前对话的最终回复。"
)
_LOGGER = log.wrapper("[llm_chat]")


def _response_content(response: litellm.ModelResponse) -> str:
    if not response.choices:
        return ""
    content = response.choices[0].message.content
    return content if isinstance(content, str) else ""


def _has_visible_reply(response: litellm.ModelResponse) -> bool:
    visible = strip_internal_media_records(_response_content(response)).strip()
    return bool(visible and visible != _END_OF_RESPONSE)


def _without_invalid_tail(messages: list[ChatMessage]) -> list[ChatMessage]:
    if not messages:
        return messages
    last = messages[-1]
    if last.get("role") == "assistant" and not last.get("tool_calls"):
        return messages[:-1]
    return messages


async def generate_chat_response(
    messages: list[ChatMessage],
    *,
    system: str,
    model: str | None,
    channel_id: str,
    ctx: Contexts,
    web_limits: WebAccessLimits,
    delivery_state: DeliveryState,
    request_timeout: float = 90.0,
) -> litellm.ModelResponse:
    """Generate normally, then recover once without tools when no visible reply exists."""

    try:
        with llm_chat_web_access_scope(web_limits), llm_chat_delivery_scope(delivery_state):
            response = cast(
                litellm.ModelResponse,
                await llm.generate(
                    messages,
                    system=system,
                    model=model,
                    ctx=ctx,
                    timeout=request_timeout,
                ),
            )
    except RuntimeError as exc:
        if str(exc) != _TOOL_LOOP_EXHAUSTED:
            raise
    else:
        if delivery_state.delivery_attempts or _has_visible_reply(response):
            return response
        _LOGGER.warning("model returned no visible reply; retrying once without tools")
        return await _finalize_without_tools(
            _without_invalid_tail(messages),
            system=system,
            model=model,
            channel_id=channel_id,
            suffix=_VISIBLE_RETRY_SUFFIX,
            require_visible=True,
            request_timeout=request_timeout,
        )

    return await _finalize_without_tools(
        messages,
        system=system,
        model=model,
        channel_id=channel_id,
        suffix=_FINALIZATION_SUFFIX,
        require_visible=delivery_state.delivery_attempts == 0,
        request_timeout=request_timeout,
    )


async def _finalize_without_tools(
    messages: list[ChatMessage],
    *,
    system: str,
    model: str | None,
    channel_id: str,
    suffix: str,
    require_visible: bool,
    request_timeout: float,
) -> litellm.ModelResponse:
    conf = get_model_config(model, channel_id)
    excluded_extra = {"tools", "tool_choice", "response_format", "timeout"}
    extra = {key: value for key, value in conf.extra.items() if key not in excluded_extra}
    response = await litellm.acompletion(
        model=conf.name,
        messages=[
            {"role": "system", "content": f"{system}\n\n{suffix}"},
            *messages,
        ],
        base_url=conf.base_url,
        api_key=conf.api_key,
        timeout=request_timeout,
        **extra,
    )
    completion = cast(litellm.ModelResponse, response)
    content = _response_content(completion)
    if not content.strip() or (require_visible and not _has_visible_reply(completion)):
        raise RuntimeError(_FINALIZATION_FAILED)
    return completion
