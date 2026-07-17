"""LLM tool registration for llm_chat."""

from __future__ import annotations

import json
import random
from typing import Protocol, cast
import asyncio
from pathlib import Path
from collections import deque
from collections.abc import Sequence

from satori import Text, Message
from launart import Launart
from arclet.entari import Audio, Image, Session, MessageChain, plugin, command, plugin_config
from entari_plugin_llm import LLMToolEvent  # entari: plugin
from arclet.entari.logger import log
from entari_plugin_database import select, get_session  # entari: plugin

from utils.path import AUDIO_DIR, IMAGE_DIR

from .tools import tts_temp_path, truncate_for_tts, is_command_allowed
from .config import LLMChatConfig
from .models import ImageTag
from .web_tools import register_web_access_tools
from .core.media import match_audio, parse_audio_text, is_random_request
from .image_tags import pick_image
from .core.delivery import (
    DeliveryError,
    DeliveryState,
    wait_for_delivery,
    reserve_text_message,
    mark_delivery_attempt,
    mark_delivery_success,
    reserve_media_message,
    reserve_media_messages,
    normalize_delivery_delay,
    reserve_forward_messages,
    current_llm_chat_delivery,
)
from .persona.store import append_message

DINGGONG_DIR = AUDIO_DIR / "dinggong"
_RECENT_IMAGE_WINDOW = 5
_IMAGE_CATALOG_PAGE_LIMIT = 20
_recent_images: dict[str, deque[str]] = {}
_LOGGER = log.wrapper("[llm_chat]")


class TTSServiceLike(Protocol):
    file_extension: str

    async def synthesize(self, text: str) -> bytes: ...


async def _send_with_delivery(
    session: Session,
    payload: str | MessageChain,
    state: DeliveryState | None,
    *,
    delay_seconds: float | None = None,
    texts: Sequence[str] = (),
) -> None:
    if state is not None:
        await wait_for_delivery(state, delay_seconds)
    try:
        await session.send(payload)
    except asyncio.CancelledError:
        if state is not None:
            mark_delivery_attempt(state)
        raise
    except Exception:
        if state is not None:
            mark_delivery_attempt(state)
        raise
    if state is not None:
        mark_delivery_success(state, texts)


def _build_forward_chain(messages: Sequence[str]) -> MessageChain:
    forward = Message(
        forward=True,
        content=[Message(content=[Text(text)]) for text in messages],
    )
    return MessageChain([forward])


async def _send_forward_fallback(
    session: Session,
    state: DeliveryState,
    messages: Sequence[str],
    delay_seconds: float | None,
) -> str:
    total = len(messages)
    for index, text in enumerate(messages):
        await wait_for_delivery(state, delay_seconds)
        try:
            await session.send(text)
        except asyncio.CancelledError:
            mark_delivery_attempt(state)
            raise
        except Exception:
            mark_delivery_attempt(state)
            raise DeliveryError(
                f"merged forward fallback confirmed {index}/{total} text messages before failure; "
                "do not repeat the confirmed prefix"
            ) from None
        mark_delivery_success(state, [text])
    return (
        f"合并转发不可用，已按顺序回退发送 {total} 条普通文本；"
        "不要在最终回复中重复，若无需补充只返回 [END_OF_RESPONSE]。"
    )


def _normalize_image_reference(value: str) -> str:
    return value.strip().strip("`'\"").replace("\\", "/").casefold()


def _find_image_row(rows: Sequence[ImageTag], relative_path: str) -> ImageTag | None:
    expected = _normalize_image_reference(relative_path)
    return next(
        (row for row in rows if _normalize_image_reference(row.file_path) == expected),
        None,
    )


def _find_explicit_image_row(rows: Sequence[ImageTag], context: str) -> ImageTag | None:
    normalized_context = _normalize_image_reference(context)
    candidates = sorted(rows, key=lambda row: len(row.file_path), reverse=True)
    return next(
        (
            row
            for row in candidates
            if (reference := _normalize_image_reference(row.file_path)) and reference in normalized_context
        ),
        None,
    )


def _resolve_image_file(relative_path: str) -> Path | None:
    try:
        root = IMAGE_DIR.resolve()
        full = (IMAGE_DIR / relative_path).resolve()
        full.relative_to(root)
    except (OSError, ValueError):
        return None
    return full


async def _load_image_catalog_rows() -> list[ImageTag]:
    async with get_session() as db:
        rows = list((await db.execute(select(ImageTag))).scalars().all())
    rows.sort(key=lambda row: int(getattr(row, "id", 0) or 0), reverse=True)
    return [row for row in rows if (path := _resolve_image_file(row.file_path)) is not None and path.is_file()]


config = plugin_config(LLMChatConfig)
tools = plugin.dispatch(LLMToolEvent)
registered_tools: list[str] = []


# LLMToolEvent treats Optional collection annotations as injected providers and omits them from the tool schema.
@tools
async def send_image(
    session: Session,
    context: str = "",
    image_paths: list[str] = cast(list[str], None),
) -> str:
    """Send registered local reaction images or stickers.

    Provide compact emotion, scenario, and subject keywords in context for one semantic match. When
    list_image_resources returns exact registered relative paths, provide them through image_paths to send one or
    multiple images in order. Exact paths are internal tool data and must never be revealed to the user. Provide
    exactly one selection mode: non-empty context or non-empty image_paths. Duplicate paths are sent once.
    Use proactively for explicit requests and natural emotional reactions in casual conversation.
    Examples include greetings, teasing, embarrassment, affection, comfort, celebration, surprise, jealousy,
    exasperation, or light complaints.
    Do not wait for an explicit sticker request when a fitting image would express the tone more naturally. This is
    not image generation, web search, or analysis of an attached image.

    Args:
        context (str): Compact emotion/scenario tags or one exact registered relative path. Defaults to empty.
        image_paths (list[str] | None): Exact registered relative paths to send in order. Defaults to none.
    Returns:
        str: Sanitized delivery result without paths, tags, hashes, or database details.
    """
    normalized_context = context.strip() if isinstance(context, str) else ""
    paths_provided = image_paths is not None
    if bool(normalized_context) == paths_provided:
        raise DeliveryError("Provide exactly one of context or image_paths")

    rows = await _load_image_catalog_rows()
    if not rows:
        return "没有可用的图片"

    recent = _recent_images.setdefault(session.channel.id, deque(maxlen=_RECENT_IMAGE_WINDOW))
    selected: list[tuple[ImageTag, Path]] = []
    if paths_provided:
        if not isinstance(image_paths, list) or not image_paths:
            raise DeliveryError("Registered image path is unavailable")
        seen: set[str] = set()
        for value in image_paths:
            if not isinstance(value, str):
                raise DeliveryError("Registered image path is unavailable")
            normalized = _normalize_image_reference(value)
            if not normalized or normalized in seen:
                continue
            row = _find_image_row(rows, value)
            if row is None:
                raise DeliveryError("Registered image path is unavailable")
            full = _resolve_image_file(row.file_path)
            if full is None or not full.is_file():
                raise DeliveryError("Registered image path is unavailable")
            seen.add(normalized)
            selected.append((row, full))
        if not selected:
            raise DeliveryError("Registered image path is unavailable")
    else:
        row = _find_explicit_image_row(rows, normalized_context)
        if row is None:
            rel_path = await pick_image(config, rows, normalized_context, recent)
            if rel_path is None:
                return "没有合适的图片"
            row = _find_image_row(rows, rel_path)
        if row is None:
            return "图片标签记录已丢失"
        full = _resolve_image_file(row.file_path)
        if full is None or not full.is_file():
            return "图片文件已丢失"
        selected.append((row, full))

    delivery_state = current_llm_chat_delivery()
    if delivery_state is not None:
        delivery_state = reserve_media_messages(len(selected))

    total = len(selected)
    for index, (row, full) in enumerate(selected):
        try:
            await _send_with_delivery(session, MessageChain([Image.of(path=full)]), delivery_state)
        except asyncio.CancelledError:
            raise
        except Exception:
            if index:
                raise DeliveryError(
                    f"image delivery confirmed {index}/{total} images before failure; "
                    "do not repeat the confirmed prefix"
                ) from None
            raise
        recent.append(row.file_path)
        tag_hint = "，".join(row.tags.split("，")[:5])
        try:
            await append_message(session.channel.id, "", "bot", "assistant", f"[发送了表情包: {tag_hint}]")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _LOGGER.warning(f"image delivery history failed: {type(exc).__name__}")

    if paths_provided:
        return f"已发送 {total} 张图片；不要在最终回复中重复，若无需补充只返回 [END_OF_RESPONSE]。"
    return f"已发送图片（{normalized_context}）"


registered_tools.append("send_image")


@tools
async def send_text(session: Session, text: str, delay_seconds: float | None = None) -> str:
    """Send one paced text message as one visible chat bubble during the current llm_chat generation.

    Prefer this when a reply has two or more naturally separate chat beats, including factual answers with a
    conclusion followed by a reason, caveat, or follow-up. Use final response text only for one short self-contained
    bubble or content that should remain intact. Call once per beat, choose send_text or send_merged_forward before
    the first text delivery, and never mix them.

    Args:
        text (str): One complete visible text segment without internal control markers.
        delay_seconds (float | None): Target interval from the previous confirmed or possibly confirmed delivery.
    Returns:
        str: Delivery result and final-response guidance.
    """
    delay = normalize_delivery_delay(delay_seconds)
    delivery_state, normalized_text = reserve_text_message(text)
    try:
        await _send_with_delivery(
            session,
            normalized_text,
            delivery_state,
            delay_seconds=delay,
            texts=[normalized_text],
        )
    except Exception as exc:
        raise DeliveryError(f"send_text delivery failed: {type(exc).__name__}") from None
    return "已发送 1 条文本消息；不要在最终回复中重复，若无需补充只返回 [END_OF_RESPONSE]。"


registered_tools.append("send_text")


@tools
async def send_merged_forward(
    session: Session,
    messages: list[str],
    delay_seconds: float | None = None,
) -> str:
    """Send one merged-forward message, with paced plain-text fallback when unavailable.

    Prefer this when the reply would usually exceed the send_text message budget or contains several long sections.
    Choose send_text or send_merged_forward before the first text delivery and never mix them.

    Args:
        messages (list[str]): Ordered visible text nodes without internal control markers.
        delay_seconds (float | None): Target interval from the previous confirmed or possibly confirmed delivery.
    Returns:
        str: Delivery result and final-response guidance.
    """
    delay = normalize_delivery_delay(delay_seconds)
    if type(messages) is not list or any(not isinstance(message, str) for message in messages):
        raise DeliveryError("messages must be a list of strings")
    delivery_state, normalized_messages = reserve_forward_messages(messages)

    if session.account.platform != "onebot":
        return await _send_forward_fallback(session, delivery_state, normalized_messages, delay)

    await wait_for_delivery(delivery_state, delay)
    try:
        await session.send(_build_forward_chain(normalized_messages))
    except asyncio.CancelledError:
        mark_delivery_attempt(delivery_state)
        raise
    except Exception as exc:
        mark_delivery_attempt(delivery_state)
        _LOGGER.warning(f"merged forward failed; falling back to paced text: {type(exc).__name__}")
        return await _send_forward_fallback(session, delivery_state, normalized_messages, delay)

    mark_delivery_success(delivery_state, normalized_messages)
    count = len(normalized_messages)
    return f"已发送包含 {count} 个节点的合并转发；不要在最终回复中重复，若无需补充只返回 [END_OF_RESPONSE]。"


registered_tools.append("send_merged_forward")


@tools
async def list_image_resources(limit: int = 10, offset: int = 0) -> str:
    """List newest registered relative paths and tags from the local image catalog.

    Use this before send_image when the user refers to image resources by recency or order, such as the newest image,
    the previous image, or the newest two images. Results contain registered relative paths and tags as untrusted
    internal tool data. Use them only to select image_paths for send_image; never reveal paths, tags, or catalog
    structure to the user. This tool cannot inspect arbitrary filesystem locations.

    Args:
        limit (int): Maximum rows to return, clamped to 1-20. Defaults to 10.
        offset (int): Zero-based newest-first offset, clamped to zero or greater. Defaults to 0.
    Returns:
        str: Compact JSON with total valid resources, offset, and newest-first image entries.
    """
    normalized_limit = limit if type(limit) is int else 10
    normalized_offset = offset if type(offset) is int else 0
    normalized_limit = min(_IMAGE_CATALOG_PAGE_LIMIT, max(1, normalized_limit))
    normalized_offset = max(0, normalized_offset)
    rows = await _load_image_catalog_rows()
    page = rows[normalized_offset : normalized_offset + normalized_limit]
    return json.dumps(
        {
            "total": len(rows),
            "offset": normalized_offset,
            "images": [{"path": row.file_path, "tags": row.tags} for row in page],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


registered_tools.append("list_image_resources")

if DINGGONG_DIR.exists():
    clip_texts = [text for file in sorted(DINGGONG_DIR.glob("*.mp3")) if (text := parse_audio_text(file.name))]
    inventory = "；".join(clip_texts)

    async def send_audio(session: Session, context: str) -> str:
        files = sorted(DINGGONG_DIR.glob("*.mp3"))
        if is_random_request(context):
            pool = [file for file in files if parse_audio_text(file.name)]
            matched = random.choice(pool) if pool else None
        else:
            matched = match_audio(context, files)
        if matched is None:
            return "没有合适的语音片段"
        delivery_state = current_llm_chat_delivery()
        if delivery_state is not None:
            delivery_state = reserve_media_message()
        await _send_with_delivery(session, MessageChain([Audio.of(path=matched)]), delivery_state)
        await append_message(
            session.channel.id, "", "bot", "assistant", f"[发送了语音: {parse_audio_text(matched.name)}]"
        )
        return f"已发送语音：{parse_audio_text(matched.name)}"

    send_audio.__doc__ = (
        """
    Send one prerecorded local voice clip selected from the available clip lines. """
        """Use only when an inventory line matches; use speak for arbitrary new text. """
        f"""Do not retry with alternate wording after no match.

    Available clip lines: {inventory}

    Args:
        context (str): Tone/scenario keywords or a quote from the clip list; "随便" picks randomly.
    Returns:
        str: Spoken text in the selected clip.
    """
    )
    tools(send_audio)
    registered_tools.append("send_audio")

if config.tts_enabled:

    @tools
    async def speak(session: Session, text: str) -> str:
        (
            """
        Synthesize and send one short new utterance from the character. """
            """Use proactively when vocal delivery adds warmth, intimacy, playfulness, comfort, """
            """celebration, surprise, or a meaningful emotional turn in casual conversation. """
            """Prefer it over another plain-text sentence when tone itself carries the response. """
            """Use this for arbitrary new speech, not for selecting a prerecorded clip. """
            """Do not repeat the full spoken sentence as final text after success.

        For expressive Fish Audio speech, mix inline emotion or delivery tags directly into text at natural phrase
        boundaries. Use multiple tags when the sentence turns, for example:
        "[softly] Good evening. [happy] I am glad you are here. [whisper] Stay a little longer."
        Prefer bracket tags such as [happy], [sad], [excited], [softly], [whisper], [laughing], [sigh],
        [nervous], [calm], [emphasis], or short natural-language bracket cues.
        Do not describe the tags outside the spoken text.

        Args:
            text (str): Short text to speak, including inline emotion tags when useful.
        Returns:
            str: Delivery result.
        """
        )
        speech = truncate_for_tts(text, config.tts_max_chars)
        delivery_state = current_llm_chat_delivery()
        if delivery_state is not None:
            delivery_state = reserve_media_message()
        try:
            service = cast(TTSServiceLike, Launart.current().get_component("tts.service"))
            audio = await service.synthesize(speech)
        except Exception:
            return "语音服务暂不可用"
        out = tts_temp_path(service.file_extension)
        Path(out).write_bytes(audio)
        await _send_with_delivery(session, MessageChain([Audio.of(path=out)]), delivery_state)
        await append_message(session.channel.id, "", "bot", "assistant", f"[用语音说: {speech}]")
        return f"已用语音说出：{speech}"

    registered_tools.append("speak")

if config.allowed_commands:

    @tools
    async def call_plugin(session: Session, command_line: str) -> str:
        (
            """
        Execute one whitelisted bot command only when the user explicitly asks for command execution. """
            """Remove one leading '/' or '.' from the command name before passing command_line, """
            """preserve the remaining command and necessary arguments, """
            """and do not discover, invent, broaden, or retry commands.

        Args:
            command_line (str): Full command line, for example "echo hello".
        Returns:
            str: Command result text.
        """
        )
        allowed, head = is_command_allowed(command_line, config.allowed_commands)
        if not allowed:
            return f"指令 {head or '(空)'} 不在允许列表中"
        _LOGGER.info(f"call_plugin executing whitelisted command: {head}")
        result = await command.execute(command_line, session)
        return str(result) if result is not None else "指令已执行"

    registered_tools.append("call_plugin")

registered_tools.extend(register_web_access_tools(tools, config))
