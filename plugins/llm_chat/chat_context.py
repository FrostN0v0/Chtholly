"""Import-light helpers for chat message assembly."""

from __future__ import annotations

import json
from typing import Any
import asyncio
from collections.abc import Callable, Sequence

import litellm
from arclet.entari import Image, Session, MessageChain

from .config import LLMChatConfig
from .models import Conversation
from .vision import describe_image, fetch_image_data_url
from .core.eval import EvalMessage, EvalConversation
from .core.media import format_image_note, sanitize_assistant_history
from .core.errors import summarize_exception
from .core.forward import ForwardedMessage, render_forwarded_storage


def serialize_user_turn(
    user_name: str,
    content: str,
    forwarded_messages: Sequence[ForwardedMessage] = (),
) -> str:
    """Serialize one user turn as unambiguous speaker/content JSON data."""
    payload: dict[str, object] = {"speaker": user_name, "content": content}
    if forwarded_messages:
        payload["forwarded_messages"] = list(forwarded_messages)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def build_chat_messages(
    history: Sequence[Conversation],
    user_name: str,
    content: str,
    current_content: str | list[dict[str, Any]] | None = None,
    current_forwarded_messages: Sequence[ForwardedMessage] = (),
) -> list[dict[str, Any]]:
    """Convert stored history plus the current user turn into LLM messages."""
    messages: list[dict[str, Any]] = []
    for row in history:
        if row.role == "assistant":
            assistant_content = sanitize_assistant_history(row.content)
            if assistant_content:
                messages.append({"role": "assistant", "content": assistant_content})
            continue
        messages.append(
            {
                "role": "user",
                "content": serialize_user_turn(row.user_name, row.content),
            }
        )
    messages.append(
        {
            "role": "user",
            "content": (
                current_content
                if current_content is not None
                else serialize_user_turn(user_name, content, current_forwarded_messages)
            ),
        }
    )
    return messages


def build_eval_conversation(
    history: Sequence[Conversation],
    user_id: str,
    user_name: str,
    content: str,
    reply: str,
) -> EvalConversation:
    """Separate recent history from the evaluator's current-turn evidence."""
    recent_history: list[EvalMessage] = []
    for row in history:
        if row.role == "assistant":
            assistant_content = sanitize_assistant_history(row.content)
            if assistant_content:
                recent_history.append(
                    {
                        "role": "assistant",
                        "speaker": "bot",
                        "target": False,
                        "content": assistant_content,
                    }
                )
            continue
        recent_history.append(
            {
                "role": "user",
                "speaker": row.user_name,
                "target": row.user_id == user_id,
                "content": row.content,
            }
        )
    assistant: EvalMessage | None = (
        {
            "role": "assistant",
            "speaker": "bot",
            "target": False,
            "content": reply,
        }
        if reply and reply != "[END_OF_RESPONSE]"
        else None
    )
    return {
        "recent_history": recent_history,
        "current_turn": {
            "user": {
                "role": "user",
                "speaker": user_name,
                "target": True,
                "content": content,
            },
            "assistant": assistant,
        },
    }


def collect_top_level_images(elements: MessageChain) -> list[Image]:
    """Collect images that are direct children of one message chain."""
    return [element for element in elements if isinstance(element, Image)]


def collect_quoted_images(session: Session) -> list[Image]:
    """Collect top-level images from the hydrated reply or quote fallback."""
    reply = getattr(session, "reply", None)
    if reply is not None:
        return collect_top_level_images(MessageChain(reply.origin.message))
    quote = session.quote
    if quote is None or not quote.children:
        return []
    return collect_top_level_images(MessageChain(quote.children))


def collect_message_images(session: Session) -> list[tuple[Image, bool]]:
    """Collect direct images first, then quoted images."""
    direct = collect_top_level_images(session.elements)
    quoted = collect_quoted_images(session)
    return [(img, False) for img in direct] + [(img, True) for img in quoted]


def model_supports_image_input(model_name: str | None) -> bool:
    """Return whether the chat model can receive images directly."""
    if not model_name:
        return False
    try:
        return bool(litellm.supports_vision(model=model_name))
    except Exception:
        return False


async def build_multimodal_user_content(
    config: LLMChatConfig,
    session: Session,
    user_name: str,
    text: str,
    warn: Callable[[str], None],
    forwarded_messages: Sequence[ForwardedMessage] = (),
) -> tuple[list[dict[str, Any]] | str, str]:
    """Build direct image_url content for vision-capable chat models plus safe stored text."""
    ordered = collect_message_images(session) if config.image_understanding_enabled else []
    cap = max(0, config.image_describe_max_per_message)
    attached = ordered[:cap]
    overflow = ordered[cap:]
    stored_parts = [text] if text else []
    content_parts: list[dict[str, Any]] = [
        {"type": "text", "text": serialize_user_turn(user_name, text, forwarded_messages)}
    ]
    has_image_payload = False

    for img, quoted in attached:
        marker = format_image_note("", quoted=quoted)
        stored_parts.append(marker)
        content_parts.append({"type": "text", "text": marker})
        data_url = await fetch_image_data_url(session, img.src)
        if data_url is None:
            warn("image passthrough skipped: image data unavailable")
            continue
        content_parts.append({"type": "image_url", "image_url": {"url": data_url}})
        has_image_payload = True

    for _img, quoted in overflow:
        marker = format_image_note("", quoted=quoted)
        stored_parts.append(marker)
        content_parts.append({"type": "text", "text": marker})

    current_text = " ".join(stored_parts)
    stored_text = render_forwarded_storage(current_text, forwarded_messages)
    if has_image_payload:
        return content_parts, stored_text
    return serialize_user_turn(user_name, current_text, forwarded_messages), stored_text


async def build_image_notes(config: LLMChatConfig, session: Session, warn: Callable[[str], None]) -> list[str]:
    """Describe inbound images and return compact context markers."""
    if not config.image_understanding_enabled:
        return []
    ordered = collect_message_images(session)
    cap = max(0, config.image_describe_max_per_message)
    described = ordered[:cap]
    overflow = ordered[cap:]

    async def note(img: Image, is_quoted: bool) -> str:
        try:
            description = await describe_image(config, session, img.src)
        except Exception as exc:
            warn(f"image describe failed: {summarize_exception(exc)}")
            description = ""
        return format_image_note(description, quoted=is_quoted)

    notes = list(await asyncio.gather(*(note(img, quoted) for img, quoted in described)))
    notes += [format_image_note("", quoted=quoted) for _img, quoted in overflow]
    return notes
