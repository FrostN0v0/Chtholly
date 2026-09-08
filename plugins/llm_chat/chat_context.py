"""Import-light helpers for chat message assembly."""

from __future__ import annotations

import json
from typing import Any
import asyncio
from dataclasses import dataclass
from collections.abc import Callable, Sequence

import litellm
from arclet.entari import Image, Author, Session, MessageChain

from .config import LLMChatConfig
from .models import Conversation
from .vision import describe_image, fetch_image_data_url
from .core.eval import EvalMessage, EvalConversation
from .core.media import format_image_note, sanitize_assistant_history
from .perception import MentionedParticipant
from .core.errors import summarize_exception
from .core.forward import ForwardedMessage, ForwardedSpeakerRole, render_forwarded_storage

_RECENT_MESSAGE_PHRASES = ("前几条消息", "前面几条消息", "最近几条消息")
_CHANNEL_SCOPE_TERMS = ("大家", "群里", "群内", "群友")
_CHANNEL_RECENCY_TERMS = ("刚刚", "刚才", "最近", "方才")
_CHANNEL_ACTIVITY_TERMS = ("聊", "说", "发", "消息", "发生", "干嘛", "做什么")


def requests_recent_channel_context(text: str) -> bool:
    """Return whether the current turn explicitly asks about recent channel activity."""
    normalized = "".join(text.split()).casefold()
    if any(phrase in normalized for phrase in _RECENT_MESSAGE_PHRASES):
        return True
    return (
        any(term in normalized for term in _CHANNEL_SCOPE_TERMS)
        and any(term in normalized for term in _CHANNEL_RECENCY_TERMS)
        and any(term in normalized for term in _CHANNEL_ACTIVITY_TERMS)
    )


def serialize_user_turn(
    user_name: str,
    content: str,
    forwarded_messages: Sequence[ForwardedMessage] = (),
    mentioned_participants: Sequence[MentionedParticipant] = (),
) -> str:
    """Serialize one user turn as unambiguous structured JSON data."""
    payload: dict[str, object] = {"speaker": user_name, "content": content}
    if mentioned_participants:
        payload["mentioned_participants"] = list(mentioned_participants)
    if forwarded_messages:
        payload["forwarded_messages"] = list(forwarded_messages)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def build_chat_messages(
    history: Sequence[Conversation],
    user_name: str,
    content: str,
    current_content: str | list[dict[str, Any]] | None = None,
    current_forwarded_messages: Sequence[ForwardedMessage] = (),
    current_mentioned_participants: Sequence[MentionedParticipant] = (),
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
                else serialize_user_turn(
                    user_name,
                    content,
                    current_forwarded_messages,
                    current_mentioned_participants,
                )
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


@dataclass(frozen=True, slots=True)
class _QuotedMessageContext:
    elements: MessageChain
    speaker: str
    speaker_role: ForwardedSpeakerRole


def _identity_values(*values: object) -> set[str]:
    return {str(value).strip().casefold() for value in values if value is not None and str(value).strip()}


def _quoted_message_context(session: Session) -> _QuotedMessageContext | None:
    reply = getattr(session, "reply", None)
    if reply is not None:
        origin = reply.origin
        elements = MessageChain(origin.message)
        member = getattr(origin, "member", None)
        user = getattr(origin, "user", None)
        quote = getattr(reply, "quote", None)
    else:
        quote = session.quote
        if quote is None or not quote.children:
            return None
        elements = MessageChain(quote.children)
        member = None
        user = None

    author = (
        next((element for element in quote.children if isinstance(element, Author)), None)
        if quote is not None
        else None
    )
    account = getattr(session, "account", None)
    self_info = getattr(account, "self_info", None)
    self_user = getattr(self_info, "user", None)
    self_ids = _identity_values(getattr(account, "self_id", None), getattr(self_user, "id", None))
    self_names = _identity_values(getattr(self_user, "name", None), getattr(self_user, "nick", None))
    user_id = getattr(user, "id", None)
    author_id = author.id if author else None
    if user is not None:
        is_self = bool(self_ids & _identity_values(user_id))
    else:
        author_values = _identity_values(author_id, author.name if author else None)
        is_self = bool(author_values & (self_ids | self_names))
    if is_self:
        speaker = "bot"
        speaker_role: ForwardedSpeakerRole = "assistant"
    else:
        known_speaker = (
            getattr(member, "nick", None)
            or getattr(user, "nick", None)
            or getattr(user, "name", None)
            or (author.name if author else None)
        )
        if known_speaker:
            speaker = str(known_speaker)
            speaker_role = "participant"
        elif user is not None or author is not None:
            speaker = "Unknown sender"
            speaker_role = "participant"
        else:
            speaker = "Unknown sender"
            speaker_role = "unknown"
    return _QuotedMessageContext(elements, speaker, speaker_role)


def collect_quoted_message(session: Session) -> ForwardedMessage | None:
    """Collect one ordinary reply as attribution-safe structured context."""
    context = _quoted_message_context(session)
    if context is None:
        return None
    parts: list[str] = []
    content = context.elements.extract_plain_text().strip()
    if content:
        parts.append(content)
    parts.extend("[Image]" for _image in collect_top_level_images(context.elements))
    if not parts:
        return None
    return {
        "speaker": context.speaker,
        "speaker_role": context.speaker_role,
        "content": " ".join(parts),
        "source": "quoted",
    }


def collect_quoted_images(session: Session) -> list[Image]:
    """Collect top-level images from the hydrated reply or quote fallback."""
    context = _quoted_message_context(session)
    return collect_top_level_images(context.elements) if context is not None else []


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
    mentioned_participants: Sequence[MentionedParticipant] = (),
) -> tuple[list[dict[str, Any]] | str, str]:
    """Build direct image_url content for vision-capable chat models plus safe stored text."""
    ordered = collect_message_images(session) if config.image_understanding_enabled else []
    quoted_context = _quoted_message_context(session)
    quoted_role = quoted_context.speaker_role if quoted_context is not None else None
    cap = max(0, config.image_describe_max_per_message)
    attached = ordered[:cap]
    overflow = ordered[cap:]
    stored_parts = [text] if text else []
    content_parts: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": serialize_user_turn(
                user_name,
                text,
                forwarded_messages,
                mentioned_participants,
            ),
        }
    ]
    has_image_payload = False

    for img, quoted in attached:
        marker = format_image_note("", quoted=quoted, quoted_role=quoted_role if quoted else None)
        stored_parts.append(marker)
        content_parts.append({"type": "text", "text": marker})
        data_url = await fetch_image_data_url(session, img.src)
        if data_url is None:
            warn("image passthrough skipped: image data unavailable")
            continue
        content_parts.append({"type": "image_url", "image_url": {"url": data_url}})
        has_image_payload = True

    for _img, quoted in overflow:
        marker = format_image_note("", quoted=quoted, quoted_role=quoted_role if quoted else None)
        stored_parts.append(marker)
        content_parts.append({"type": "text", "text": marker})

    current_text = " ".join(stored_parts)
    stored_text = render_forwarded_storage(current_text, forwarded_messages)
    if has_image_payload:
        return content_parts, stored_text
    return (
        serialize_user_turn(
            user_name,
            current_text,
            forwarded_messages,
            mentioned_participants,
        ),
        stored_text,
    )


async def build_image_notes(config: LLMChatConfig, session: Session, warn: Callable[[str], None]) -> list[str]:
    """Describe inbound images and return compact context markers."""
    if not config.image_understanding_enabled:
        return []
    ordered = collect_message_images(session)
    quoted_context = _quoted_message_context(session)
    quoted_role = quoted_context.speaker_role if quoted_context is not None else None
    cap = max(0, config.image_describe_max_per_message)
    described = ordered[:cap]
    overflow = ordered[cap:]

    async def note(img: Image, is_quoted: bool) -> str:
        try:
            description = await describe_image(config, session, img.src)
        except Exception as exc:
            warn(f"image describe failed: {summarize_exception(exc)}")
            description = ""
        return format_image_note(
            description,
            quoted=is_quoted,
            quoted_role=quoted_role if is_quoted else None,
        )

    notes = list(await asyncio.gather(*(note(img, quoted) for img, quoted in described)))
    notes += [
        format_image_note("", quoted=quoted, quoted_role=quoted_role if quoted else None) for _img, quoted in overflow
    ]
    return notes
