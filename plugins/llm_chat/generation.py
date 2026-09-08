"""Chat generation orchestration with bounded tool-free recovery."""

from __future__ import annotations

import json
import time
from typing import Any, cast
import asyncio
from contextlib import nullcontext
from collections.abc import Callable, Awaitable

import litellm
from agno.media import Image as AgnoImage
from arclet.letoderea import Contexts
from entari_plugin_llm import GenericResponse, llm  # entari: plugin
from agno.models.message import Message as AgnoMessage
from arclet.entari.logger import log
from entari_plugin_llm.config import get_model_config

from .core.media import has_meaningful_text, strip_internal_media_records
from .core.types import ChatMessage
from .web.policy import WebAccessLimits, llm_chat_web_access_scope
from .agno_compat import agno_delivery_tool_scope, agno_tool_call_limit_scope, recommended_tool_call_limit
from .core.errors import is_moderation_empty_choices_error
from .agent_context import AgentAccessContext, agent_access_scope
from .core.delivery import DeliveryState, llm_chat_delivery_scope, strip_trailing_end_of_response
from .channel_images import (
    ChannelImageReferences,
    llm_chat_channel_image_scope,
)
from .core.tool_trace import ToolTraceRecorder, llm_chat_tool_trace_scope
from .image_edit_refs import (
    ImageEditReferences,
    llm_chat_image_edit_scope,
)
from .runtime_context import llm_chat_context_scope
from .core.agent_trace import AgentTurnRecorder
from .core.native_images import extract_native_images
from .core.media_delivery import (
    is_media_unavailable_reply,
    latest_user_requests_media,
    strip_media_unavailable_marker,
    latest_user_requests_webpage_screenshot,
    latest_user_requests_web_image_reference,
)
from .core.tool_trace_safety import sanitize_json

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
    "上一条候选回复不可发送：它为空、只包含空白或标点、只包含结束控制标记，或复述了历史中的媒体发送记录。"
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
_REFERENCE_EDIT_RECOVERY_SUFFIX = (
    "当前用户明确要求从公开网页取得真实视觉参考后完成图片修改。"
    "上一条候选回复没有通过 edit_image 确认发送合格结果，因此不可直接交付。"
    "必须先用 web_search/read_web_page 选择公开来源，再调用 capture_web_reference 私下抓取并检查视觉描述，"
    "确认与目标一致后把返回的 image_ref 传给 edit_image。generate_image 和模型原生图片输出不能满足本轮要求。"
    "若在剩余有界额度内无法取得可靠参考或 edit_image 未确认发送，"
    "最终普通文本必须以 [MEDIA_UNAVAILABLE] 开头并如实说明失败。"
)
_IMAGE_INPUT_REPLY_SUFFIX = (
    "当前轮用户只发送或引用了图片，没有提供有意义的文字。不得把空文本解释为句号、一个点、沉默或无事发生；"
    "请依据当前上下文中实际可见的图片或图片描述，补充一条简短自然的文字回应。"
    "即使本轮已有媒体工具发送成功，也不得只输出 [END_OF_RESPONSE] 或复述已发送媒体；"
    "只有图片内容确实不可用时，才自然请用户重新发送原图。不得再调用任何工具。"
)
_MODERATION_RECOVERY_SUFFIX = (
    "上一次请求没有返回可用候选。此次恢复只包含最新用户轮次，历史对话、画像、记忆、会话交接和图片像素均未提供。"
    "只依据当前仍可见的文字、系统生成的图片描述和实际工具结果行动，不得推测或复述被移除内容。"
    "若用户明确要求收藏当前图片，且现有描述足以确认它是可复用的表情包、反应图或贴纸，可以调用 tag_image；"
    "描述不足或图片不适合收藏时不得调用，并自然说明本轮无法确认。任何收藏结果仍以工具返回为准。"
)
_RUNTIME_CONTEXT_OPEN = "<runtime_context>\n"
_RUNTIME_CONTEXT_CLOSE = "\n</runtime_context>"
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


def _suppress_native_images(response: object) -> None:
    """Prevent unverified provider-native images from reaching delivery."""

    try:
        setattr(response, "_llm_chat_native_images", ())
    except Exception:
        return


def _has_visible_reply(response: object) -> bool:
    visible = strip_media_unavailable_marker(strip_internal_media_records(response_content(response))).strip()
    return has_meaningful_text(strip_trailing_end_of_response(visible))


def _requires_visible_reply(delivery_state: DeliveryState, require_text_reply: bool) -> bool:
    return delivery_state.delivery_attempts == 0 or (require_text_reply and not delivery_state.delivered_texts)


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


def _isolated_latest_user_turn(messages: list[ChatMessage]) -> list[ChatMessage]:
    """Keep only the latest user turn and remove image bytes for one safe retry."""

    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            isolated_content: str | list[dict[str, Any]] = content
        elif isinstance(content, list):
            text_parts = [
                {"type": "text", "text": text}
                for part in content
                if isinstance(part, dict)
                and part.get("type") == "text"
                and isinstance((text := part.get("text")), str)
                and text
            ]
            isolated_content = text_parts or "[Image pixels unavailable in isolated retry]"
        else:
            continue
        isolated: dict[str, Any] = {"role": "user", "content": isolated_content}
        name = message.get("name")
        if isinstance(name, str) and name:
            isolated["name"] = name
        return [cast(ChatMessage, isolated)]
    raise RuntimeError("LLM isolated recovery has no current user turn")


def _moderation_recovery_system(system: str) -> str:
    """Remove user-derived runtime context while retaining trusted tool rules."""

    prefix, opening, remainder = system.partition(_RUNTIME_CONTEXT_OPEN)
    if not opening:
        return f"{system}\n\n{_MODERATION_RECOVERY_SUFFIX}"
    raw_context, closing, suffix = remainder.partition(_RUNTIME_CONTEXT_CLOSE)
    if not closing:
        return f"{system}\n\n{_MODERATION_RECOVERY_SUFFIX}"
    try:
        parsed = json.loads(raw_context)
    except ValueError:
        parsed = {}
    reply_intent = parsed.get("reply_intent", {}) if isinstance(parsed, dict) else {}
    if not isinstance(reply_intent, dict):
        reply_intent = {}
    safe_context = {
        "current_state": {},
        "current_speaker": "current user",
        "current_participant_ref": "",
        "relationship_style": "",
        "self_reference_attached": False,
        "user_profile": {},
        "relevant_memories": [],
        "agent_session": {},
        "recent_impression": "",
        "reply_intent": reply_intent,
    }
    serialized = json.dumps(safe_context, ensure_ascii=False, separators=(",", ":"))
    return (
        f"{prefix}{_RUNTIME_CONTEXT_OPEN}{serialized}{_RUNTIME_CONTEXT_CLOSE}{suffix}\n\n{_MODERATION_RECOVERY_SUFFIX}"
    )


async def _recover_moderation_empty_choices(
    isolated_messages: list[ChatMessage],
    *,
    system: str,
    model: str | None,
    request_timeout: float,
    agent_events: AgentTurnRecorder | None,
    tool_trace: ToolTraceRecorder,
) -> GenericResponse[None]:
    """Retry one moderation-empty response without historical or binary context."""

    _LOGGER.warning("provider moderation returned no choices; retrying isolated current turn once")
    return cast(
        GenericResponse[None],
        await _record_model_attempt(
            lambda: _generate_with_tools(
                isolated_messages,
                system=_moderation_recovery_system(system),
                model=model,
                request_timeout=request_timeout,
                max_retries=0,
                parallel_tool_calls=False,
            ),
            recorder=agent_events,
            tool_trace=tool_trace,
            model_name=model or "default",
        ),
    )


async def _generate_with_tools(
    messages: list[ChatMessage],
    *,
    system: str,
    model: str | None,
    request_timeout: float,
    max_retries: int | None = None,
    parallel_tool_calls: bool | None = None,
) -> GenericResponse[None]:
    request_options: dict[str, Any] = {"timeout": request_timeout}
    if max_retries is not None:
        request_options["max_retries"] = max_retries
    if parallel_tool_calls is not None:
        request_options["parallel_tool_calls"] = parallel_tool_calls
    return cast(
        GenericResponse[None],
        await llm.generate(
            cast(list[Any], messages),
            system=system,
            model=model,
            **request_options,
        ),
    )


def _response_metrics(response: object) -> object:
    metrics = getattr(response, "metrics", None) or getattr(response, "usage", None)
    if metrics is not None and hasattr(metrics, "model_dump"):
        metrics = metrics.model_dump()
    elif metrics is not None and hasattr(metrics, "dict"):
        metrics = metrics.dict()
    return sanitize_json(metrics, max_text=2000)


async def _record_model_attempt(
    run: Callable[[], Awaitable[object]],
    *,
    recorder: AgentTurnRecorder | None,
    tool_trace: ToolTraceRecorder,
    model_name: str,
) -> object:
    attempt = recorder.next_attempt() if recorder is not None else tool_trace.attempt
    tool_trace.set_attempt(attempt)
    started = time.monotonic()
    try:
        response = await run()
    except asyncio.CancelledError:
        if recorder is not None:
            recorder.record_model_attempt(
                attempt=attempt,
                model_name=model_name,
                status="cancelled",
                duration_ms=round((time.monotonic() - started) * 1000),
            )
        raise
    except Exception as exc:
        if recorder is not None:
            recorder.record_model_attempt(
                attempt=attempt,
                model_name=model_name,
                status="failed",
                duration_ms=round((time.monotonic() - started) * 1000),
                error=f"{type(exc).__name__}: {exc}",
            )
        raise
    if recorder is not None:
        recorder.record_model_attempt(
            attempt=attempt,
            model_name=model_name,
            status="succeeded",
            duration_ms=round((time.monotonic() - started) * 1000),
            content=response_content(response),
            metrics=_response_metrics(response),
        )
    return response


async def _recover_requested_media(
    messages: list[ChatMessage],
    *,
    system: str,
    model: str | None,
    delivery_state: DeliveryState,
    image_edit_references: ImageEditReferences,
    request_timeout: float,
    max_retries: int | None,
    agent_events: AgentTurnRecorder | None,
    tool_trace: ToolTraceRecorder,
) -> GenericResponse[None]:
    reference_required = image_edit_references.requires_web_reference
    _LOGGER.warning(
        "required reference image edit was not confirmed; retrying once with tools"
        if reference_required
        else "requested media was not confirmed; retrying once with tools"
    )
    try:
        with agno_tool_call_limit_scope(_MEDIA_RECOVERY_TOOL_CALL_LIMIT):
            response = cast(
                GenericResponse[None],
                await _record_model_attempt(
                    lambda: _generate_with_tools(
                        messages,
                        system=(
                            f"{system}\n\n"
                            f"{_REFERENCE_EDIT_RECOVERY_SUFFIX if reference_required else _MEDIA_RECOVERY_SUFFIX}"
                        ),
                        model=model,
                        request_timeout=request_timeout,
                        max_retries=max_retries,
                        parallel_tool_calls=False,
                    ),
                    recorder=agent_events,
                    tool_trace=tool_trace,
                    model_name=model or "default",
                ),
            )
    except RuntimeError as exc:
        if str(exc) == _TOOL_LOOP_EXHAUSTED:
            raise RuntimeError(_MEDIA_RECOVERY_FAILED) from exc
        raise
    if reference_required:
        if image_edit_references.edit_confirmed:
            _suppress_native_images(response)
            return response
    elif delivery_state.confirmed_media_deliveries > 0 or response_images(response):
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
    image_edit_references: ImageEditReferences | None = None,
    require_text_reply: bool = False,
    channel_image_references: ChannelImageReferences | None = None,
    request_timeout: float = 90.0,
    media_request_timeout: float = 300.0,
    tool_trace: ToolTraceRecorder | None = None,
    agent_events: AgentTurnRecorder | None = None,
    agent_access: AgentAccessContext | None = None,
) -> GenerationResponse:
    """Generate with a longer single-attempt timeout for explicit media requests."""

    tool_call_limit = recommended_tool_call_limit(
        web_limits.total_limit,
        delivery_state.limits.max_text_messages,
        delivery_state.limits.max_media_messages,
    )
    media_requested = latest_user_requests_media(messages)
    webpage_screenshot_requested = latest_user_requests_webpage_screenshot(messages)
    web_reference_requested = latest_user_requests_web_image_reference(messages)
    generation_timeout = media_request_timeout if media_requested else request_timeout
    generation_max_retries = 0 if media_requested else None
    tool_loop_exhausted = False
    active_tool_trace = tool_trace or ToolTraceRecorder()
    active_channel_image_references = channel_image_references or ChannelImageReferences()
    active_image_edit_references = image_edit_references or ImageEditReferences.from_input_attachments(
        (),
        requires_web_reference=web_reference_requested,
    )
    active_image_edit_references.requires_web_reference = (
        active_image_edit_references.requires_web_reference or web_reference_requested
    )
    with (
        agno_tool_call_limit_scope(tool_call_limit),
        agno_delivery_tool_scope(),
        llm_chat_web_access_scope(
            web_limits,
            allow_webpage_screenshots=webpage_screenshot_requested,
            allow_reference_capture=active_image_edit_references.requires_web_reference,
        ),
        llm_chat_delivery_scope(delivery_state),
        llm_chat_tool_trace_scope(active_tool_trace),
        llm_chat_context_scope(ctx),
        llm_chat_channel_image_scope(active_channel_image_references),
        llm_chat_image_edit_scope(active_image_edit_references),
        agent_access_scope(agent_access) if agent_access is not None else nullcontext(),
    ):
        response: GenericResponse[None] | None = None
        response_context = messages
        try:
            response = cast(
                GenericResponse[None],
                await _record_model_attempt(
                    lambda: _generate_with_tools(
                        messages,
                        system=system,
                        model=model,
                        request_timeout=generation_timeout,
                        max_retries=generation_max_retries,
                        parallel_tool_calls=False if media_requested else None,
                    ),
                    recorder=agent_events,
                    tool_trace=active_tool_trace,
                    model_name=model or "default",
                ),
            )
        except RuntimeError as exc:
            if (
                is_moderation_empty_choices_error(exc)
                and not active_tool_trace.events
                and delivery_state.delivery_attempts == 0
            ):
                response_context = _isolated_latest_user_turn(messages)
                response = await _recover_moderation_empty_choices(
                    response_context,
                    system=system,
                    model=model,
                    request_timeout=generation_timeout,
                    agent_events=agent_events,
                    tool_trace=active_tool_trace,
                )
            elif str(exc) == _TOOL_LOOP_EXHAUSTED:
                tool_loop_exhausted = True
                if media_requested and (
                    delivery_state.confirmed_media_deliveries == 0
                    or (
                        active_image_edit_references.requires_web_reference
                        and not active_image_edit_references.edit_confirmed
                    )
                ):
                    return await _recover_requested_media(
                        messages,
                        system=system,
                        model=model,
                        delivery_state=delivery_state,
                        image_edit_references=active_image_edit_references,
                        request_timeout=generation_timeout,
                        max_retries=generation_max_retries,
                        agent_events=agent_events,
                        tool_trace=active_tool_trace,
                    )
            else:
                raise

        if response is not None:
            transcript = _response_transcript(response, response_context)
            native_images = response_images(response)
            if active_image_edit_references.edit_confirmed:
                _suppress_native_images(response)
                native_images = ()
            elif native_images and active_image_edit_references.requires_web_reference:
                _suppress_native_images(response)
                return await _recover_requested_media(
                    _without_invalid_tail(transcript),
                    system=system,
                    model=model,
                    delivery_state=delivery_state,
                    image_edit_references=active_image_edit_references,
                    request_timeout=generation_timeout,
                    max_retries=generation_max_retries,
                    agent_events=agent_events,
                    tool_trace=active_tool_trace,
                )
            if native_images:
                return response
            if media_requested and (
                delivery_state.confirmed_media_deliveries == 0
                or (
                    active_image_edit_references.requires_web_reference
                    and not active_image_edit_references.edit_confirmed
                )
            ):
                return await _recover_requested_media(
                    _without_invalid_tail(transcript),
                    system=system,
                    model=model,
                    delivery_state=delivery_state,
                    image_edit_references=active_image_edit_references,
                    request_timeout=generation_timeout,
                    max_retries=generation_max_retries,
                    agent_events=agent_events,
                    tool_trace=active_tool_trace,
                )
            if _tool_call_limit_hit(response):
                _LOGGER.warning("Agno tool call limit reached; finalizing once without tools")
                visible_reply_required = _requires_visible_reply(delivery_state, require_text_reply)
                return await _finalize_without_tools(
                    transcript,
                    system=system,
                    model=model,
                    channel_id=channel_id,
                    suffix=(
                        _IMAGE_INPUT_REPLY_SUFFIX
                        if require_text_reply and not delivery_state.delivered_texts
                        else _FINALIZATION_SUFFIX
                    ),
                    require_visible=visible_reply_required,
                    request_timeout=request_timeout,
                    agent_events=agent_events,
                    tool_trace=active_tool_trace,
                )
            if _has_visible_reply(response):
                return response
            if delivery_state.delivery_attempts and not _requires_visible_reply(
                delivery_state,
                require_text_reply,
            ):
                return response
            _LOGGER.warning("model returned no visible reply; retrying once without tools")
            return await _finalize_without_tools(
                _without_invalid_tail(transcript),
                system=system,
                model=model,
                channel_id=channel_id,
                suffix=(
                    _IMAGE_INPUT_REPLY_SUFFIX
                    if require_text_reply and not delivery_state.delivered_texts
                    else _VISIBLE_RETRY_SUFFIX
                ),
                require_visible=True,
                request_timeout=request_timeout,
                agent_events=agent_events,
                tool_trace=active_tool_trace,
            )

    if not tool_loop_exhausted:
        raise RuntimeError(_TOOL_LOOP_EXHAUSTED)
    visible_reply_required = _requires_visible_reply(delivery_state, require_text_reply)
    return await _finalize_without_tools(
        messages,
        system=system,
        model=model,
        channel_id=channel_id,
        suffix=(
            _IMAGE_INPUT_REPLY_SUFFIX
            if require_text_reply and not delivery_state.delivered_texts
            else _FINALIZATION_SUFFIX
        ),
        require_visible=visible_reply_required,
        request_timeout=request_timeout,
        agent_events=agent_events,
        tool_trace=active_tool_trace,
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
    agent_events: AgentTurnRecorder | None,
    tool_trace: ToolTraceRecorder,
) -> litellm.ModelResponse:
    conf = get_model_config(model, channel_id)
    excluded_extra = {"tools", "tool_choice", "response_format", "timeout"}
    extra = {key: value for key, value in conf.extra.items() if key not in excluded_extra}
    response = await _record_model_attempt(
        lambda: litellm.acompletion(
            model=conf.name,
            messages=[
                {"role": "system", "content": f"{system}\n\n{suffix}"},
                *messages,
            ],
            base_url=conf.base_url,
            api_key=conf.api_key,
            timeout=request_timeout,
            **extra,
        ),
        recorder=agent_events,
        tool_trace=tool_trace,
        model_name=conf.name,
    )
    completion = cast(litellm.ModelResponse, response)
    content = response_content(completion)
    native_images = response_images(completion)
    if (not content.strip() and not native_images) or (
        require_visible and not _has_visible_reply(completion) and not native_images
    ):
        raise RuntimeError(_FINALIZATION_FAILED)
    return completion
