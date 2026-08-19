"""Chat generation orchestration with bounded tool-free recovery."""

from __future__ import annotations

from typing import Any, cast

import litellm
from agno.media import Image as AgnoImage
from arclet.letoderea import Contexts
from entari_plugin_llm import GenericResponse, llm  # entari: plugin
from agno.models.message import Message as AgnoMessage
from arclet.entari.logger import log
from entari_plugin_llm.config import get_model_config

from .core.media import strip_internal_media_records
from .core.types import ChatMessage
from .web.policy import WebAccessLimits, llm_chat_web_access_scope
from .agno_compat import agno_tool_call_limit_scope, recommended_tool_call_limit
from .core.delivery import DeliveryState, llm_chat_delivery_scope, strip_trailing_end_of_response
from .channel_images import (
    ChannelImageReferences,
    llm_chat_channel_image_scope,
)
from .core.tool_trace import ToolTraceRecorder, llm_chat_tool_trace_scope
from .runtime_context import llm_chat_context_scope
from .core.native_images import extract_native_images
from .core.media_delivery import (
    is_media_unavailable_reply,
    latest_user_requests_media,
    strip_media_unavailable_marker,
)

GenerationResponse = GenericResponse[None] | litellm.ModelResponse


_TOOL_LOOP_EXHAUSTED = "LLM completion did not return a response"
_FINALIZATION_FAILED = "LLM finalization did not return a response"
_MEDIA_RECOVERY_FAILED = "LLM media recovery did not confirm delivery or report unavailability"
_MEDIA_RECOVERY_TOOL_CALL_LIMIT = 8
_FINALIZATION_SUFFIX = (
    "工具调用轮次已结束。不得再调用任何工具；请仅依据本轮已返回的工具结果和已有对话直接给出最终答复。"
    "若证据不足，明确说明未核实或无法确认的部分，不得编造搜索、读取或执行结果。"
    "若已有任意发送工具成功，不得复述已发送内容；若工具错误包含 merged forward fallback confirmed 与 "
    "do not repeat the confirmed prefix，也不得复述其中已确认的前缀。"
    "若本轮没有确认发送成功，不得承诺让用户下一轮重复请求即可完成本轮未完成的发送，也不得声称届时无需重新检索。"
    "仅补充尚未发送且有依据的新信息，否则只返回 [END_OF_RESPONSE]。"
)
_VISIBLE_RETRY_SUFFIX = (
    "上一条候选回复不可发送：它为空、只包含结束控制标记，或复述了历史中的媒体发送记录。"
    "本轮没有发生任何发送尝试。不得再调用工具，不得输出媒体发送记录或 [END_OF_RESPONSE]，"
    "也不得声称已经发送媒体；请直接给出一条自然、可见且符合当前对话的最终回复。"
)
_MEDIA_RECOVERY_SUFFIX = (
    "当前用户明确要求实际发送或补发媒体，但上一条候选回复没有产生任何确认的媒体发送，因此不可直接交付。"
    "现在重新完成本轮：若能从已有上下文或剩余的有界工具额度取得合法来源，必须实际调用对应媒体发送工具，"
    "并以工具成功结果为准；不得用普通文字假装附件已经发出。"
    "若经过有界尝试仍不能确认媒体发送成功，最终普通文本必须以 [MEDIA_UNAVAILABLE] 开头，"
    "如实说明本轮未能发送，不得承诺下一轮必然成功；失败说明不得通过 send_text 或其他发送工具发送。"
)
_LOGGER = log.wrapper("[llm_chat]")


def response_content(response: object) -> str:
    content = getattr(response, "content", None)
    if isinstance(content, str):
        return content
    choices = getattr(response, "choices", None)
    if not isinstance(choices, list) or not choices:
        return ""
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", None)
    return content if isinstance(content, str) else ""


def response_images(response: object) -> tuple[AgnoImage, ...]:
    """Return safely normalized native images from one model response."""

    return extract_native_images(response)


def _has_visible_reply(response: object) -> bool:
    visible = strip_media_unavailable_marker(strip_internal_media_records(response_content(response))).strip()
    return bool(strip_trailing_end_of_response(visible))


def _agno_messages(response: object) -> list[AgnoMessage]:
    messages = getattr(response, "messages", None)
    if not isinstance(messages, list):
        return []
    return [message for message in messages if isinstance(message, AgnoMessage)]


def _tool_call_limit_hit(response: object) -> bool:
    return any(
        message.role == "tool"
        and message.tool_call_error is True
        and isinstance(message.content, str)
        and message.content.startswith("Tool call limit reached.")
        for message in _agno_messages(response)
    )


def _response_transcript(response: object, fallback: list[ChatMessage]) -> list[ChatMessage]:
    transcript: list[dict[str, Any]] = []
    for message in _agno_messages(response):
        if message.role == "system":
            continue
        if message.role == "user" and isinstance(message.content, (str, list)):
            item: dict[str, Any] = {"role": "user", "content": message.content}
            if message.name:
                item["name"] = message.name
            transcript.append(item)
            continue
        if message.role == "assistant":
            item = {"role": "assistant", "content": message.content if isinstance(message.content, str) else None}
            if message.tool_calls:
                item["tool_calls"] = message.tool_calls
            if message.reasoning_content:
                item["reasoning_content"] = message.reasoning_content
            transcript.append(item)
            continue
        if message.role == "tool" and isinstance(message.content, str) and message.tool_call_id:
            item = {"role": "tool", "content": message.content, "tool_call_id": message.tool_call_id}
            name = message.name or message.tool_name
            if name:
                item["name"] = name
            transcript.append(item)
    return cast(list[ChatMessage], transcript) if transcript else fallback


def _without_invalid_tail(messages: list[ChatMessage]) -> list[ChatMessage]:
    if not messages:
        return messages
    last = messages[-1]
    if last.get("role") == "assistant" and not last.get("tool_calls"):
        return messages[:-1]
    return messages


async def _generate_with_tools(
    messages: list[ChatMessage],
    *,
    system: str,
    model: str | None,
    request_timeout: float,
    max_retries: int | None = None,
) -> GenericResponse[None]:
    request_options: dict[str, Any] = {"timeout": request_timeout}
    if max_retries is not None:
        request_options["max_retries"] = max_retries
    return cast(
        GenericResponse[None],
        await llm.generate(
            cast(list[Any], messages),
            system=system,
            model=model,
            **request_options,
        ),
    )


async def _recover_requested_media(
    messages: list[ChatMessage],
    *,
    system: str,
    model: str | None,
    delivery_state: DeliveryState,
    request_timeout: float,
    max_retries: int | None,
) -> GenericResponse[None]:
    _LOGGER.warning("requested media was not confirmed; retrying once with tools")
    try:
        with agno_tool_call_limit_scope(_MEDIA_RECOVERY_TOOL_CALL_LIMIT):
            response = await _generate_with_tools(
                messages,
                system=f"{system}\n\n{_MEDIA_RECOVERY_SUFFIX}",
                model=model,
                request_timeout=request_timeout,
                max_retries=max_retries,
            )
    except RuntimeError as exc:
        if str(exc) == _TOOL_LOOP_EXHAUSTED:
            raise RuntimeError(_MEDIA_RECOVERY_FAILED) from exc
        raise
    if delivery_state.confirmed_media_deliveries > 0 or response_images(response):
        return response
    if is_media_unavailable_reply(response_content(response)):
        return response
    raise RuntimeError(_MEDIA_RECOVERY_FAILED)


async def generate_chat_response(
    messages: list[ChatMessage],
    *,
    system: str,
    model: str | None,
    channel_id: str,
    ctx: Contexts | None,
    web_limits: WebAccessLimits,
    delivery_state: DeliveryState,
    channel_image_references: ChannelImageReferences | None = None,
    request_timeout: float = 90.0,
    media_request_timeout: float = 300.0,
    tool_trace: ToolTraceRecorder | None = None,
) -> GenerationResponse:
    """Generate with a longer single-attempt timeout for explicit media requests."""

    tool_call_limit = recommended_tool_call_limit(
        web_limits.total_limit,
        delivery_state.limits.max_text_messages,
        delivery_state.limits.max_media_messages,
    )
    media_requested = latest_user_requests_media(messages)
    generation_timeout = media_request_timeout if media_requested else request_timeout
    generation_max_retries = 0 if media_requested else None
    tool_loop_exhausted = False
    active_tool_trace = tool_trace or ToolTraceRecorder()
    active_channel_image_references = channel_image_references or ChannelImageReferences()
    with (
        agno_tool_call_limit_scope(tool_call_limit),
        llm_chat_web_access_scope(web_limits),
        llm_chat_delivery_scope(delivery_state),
        llm_chat_tool_trace_scope(active_tool_trace),
        llm_chat_context_scope(ctx),
        llm_chat_channel_image_scope(active_channel_image_references),
    ):
        try:
            response = await _generate_with_tools(
                messages,
                system=system,
                model=model,
                request_timeout=generation_timeout,
                max_retries=generation_max_retries,
            )
        except RuntimeError as exc:
            if str(exc) != _TOOL_LOOP_EXHAUSTED:
                raise
            tool_loop_exhausted = True
            if media_requested and delivery_state.confirmed_media_deliveries == 0:
                return await _recover_requested_media(
                    messages,
                    system=system,
                    model=model,
                    delivery_state=delivery_state,
                    request_timeout=generation_timeout,
                    max_retries=generation_max_retries,
                )
        else:
            transcript = _response_transcript(response, messages)
            native_images = response_images(response)
            if native_images:
                return response
            if media_requested and delivery_state.confirmed_media_deliveries == 0:
                return await _recover_requested_media(
                    _without_invalid_tail(transcript),
                    system=system,
                    model=model,
                    delivery_state=delivery_state,
                    request_timeout=generation_timeout,
                    max_retries=generation_max_retries,
                )
            if _tool_call_limit_hit(response):
                _LOGGER.warning("Agno tool call limit reached; finalizing once without tools")
                return await _finalize_without_tools(
                    transcript,
                    system=system,
                    model=model,
                    channel_id=channel_id,
                    suffix=_FINALIZATION_SUFFIX,
                    require_visible=delivery_state.delivery_attempts == 0,
                    request_timeout=request_timeout,
                )
            if delivery_state.delivery_attempts or _has_visible_reply(response):
                return response
            _LOGGER.warning("model returned no visible reply; retrying once without tools")
            return await _finalize_without_tools(
                _without_invalid_tail(transcript),
                system=system,
                model=model,
                channel_id=channel_id,
                suffix=_VISIBLE_RETRY_SUFFIX,
                require_visible=True,
                request_timeout=request_timeout,
            )

    if not tool_loop_exhausted:
        raise RuntimeError(_TOOL_LOOP_EXHAUSTED)
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
    content = response_content(completion)
    native_images = response_images(completion)
    if (not content.strip() and not native_images) or (
        require_visible and not _has_visible_reply(completion) and not native_images
    ):
        raise RuntimeError(_FINALIZATION_FAILED)
    return completion
