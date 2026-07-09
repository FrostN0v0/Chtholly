"""Import-light helpers for chat message assembly."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence

from arclet.entari import Image, Session, MessageChain

from utils.llm_chat_core.media import format_image_note

from .config import LLMChatConfig
from .models import Conversation
from .vision import describe_image


def build_chat_messages(history: Sequence[Conversation], user_name: str, content: str) -> list[dict[str, str]]:
    """Convert stored history plus the current user line into LLM messages."""
    messages = [
        {
            "role": row.role if row.role == "assistant" else "user",
            "content": f"[{row.user_name}]: {row.content}" if row.role == "user" else row.content,
        }
        for row in history
    ]
    messages.append({"role": "user", "content": f"[{user_name}]: {content}"})
    return messages


def build_eval_transcript(
    history: Sequence[Conversation],
    user_id: str,
    user_name: str,
    content: str,
    reply: str,
) -> list[str]:
    """Build evaluator transcript lines from recent history and this turn."""

    def transcript_line(row: Conversation) -> str:
        if row.role == "assistant":
            return f"[你]: {row.content}"
        if row.user_id == user_id:
            return f"[评估对象 {row.user_name}]: {row.content}"
        return f"[{row.user_name}]: {row.content}"

    transcript = [transcript_line(row) for row in history]
    transcript.append(f"[评估对象 {user_name}]: {content}")
    if reply and reply != "[END_OF_RESPONSE]":
        transcript.append(f"[你]: {reply}")
    return transcript


def collect_message_images(session: Session, cap: int) -> list[tuple[Image, bool]]:
    """Collect direct images first, then quoted images, capped by caller policy."""
    direct = list(session.elements.select(Image))
    quote = session.quote
    quoted = list(MessageChain(quote.children).select(Image)) if quote and quote.children else []
    ordered = [(img, False) for img in direct] + [(img, True) for img in quoted]
    return ordered[: max(0, cap)] + ordered[max(0, cap) :]


async def build_image_notes(config: LLMChatConfig, session: Session, warn: Callable[[str], None]) -> list[str]:
    """Describe inbound images and return compact context markers."""
    if not config.image_understanding_enabled:
        return []
    ordered = collect_message_images(session, max(0, config.image_describe_max_per_message))
    cap = max(0, config.image_describe_max_per_message)
    described = ordered[:cap]
    overflow = ordered[cap:]

    async def note(img: Image, is_quoted: bool) -> str:
        try:
            description = await describe_image(config, session, img.src)
        except Exception as exc:
            warn(f"image describe failed: {exc!r}")
            description = ""
        return format_image_note(description, quoted=is_quoted)

    notes = list(await asyncio.gather(*(note(img, quoted) for img, quoted in described)))
    notes += [format_image_note("", quoted=quoted) for _img, quoted in overflow]
    return notes
