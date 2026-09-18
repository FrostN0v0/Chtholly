"""Import-light helpers for chat message assembly."""

from __future__ import annotations

import json
from typing import Any
from dataclasses import dataclass
from collections.abc import Callable, Sequence

import litellm
from arclet.entari import Image, Author, Session, MessageChain

from .config import LLMChatConfig
from .models import Conversation
from .vision import describe_image_bytes
from .core.media import format_image_note, sanitize_assistant_history
from .perception import MentionedParticipant
from .core.errors import summarize_exception
from .core.forward import ForwardedMessage, ForwardedSpeakerRole, render_forwarded_storage
from .image_inputs import ImageInputs, ImageInputError, current_image_inputs
from .core.image_source import fetch_image_bytes, raw_to_image_data_url

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
    input_images: Sequence[dict[str, object]] = (),
) -> str:
    """Serialize one user turn as unambiguous structured JSON data."""
    payload: dict[str, object] = {"speaker": user_name, "content": content}
    if input_images:
        payload["input_images"] = list(input_images)
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


def register_message_image_inputs(session: Session, inputs: ImageInputs) -> None:
    """Record every direct/quoted position before any image acquisition or perception."""
    for index, (image, quoted) in enumerate(collect_message_images(session), start=1):

        async def load(source: str = image.src) -> bytes:
            data = await fetch_image_bytes(session, source)
            if data is None:
                raise ImageInputError("Original image could not be acquired")
            return data

        inputs.register(
            session,
            source="quoted" if quoted else "direct",
            key=("download", image.src),
            load=load,
            index=index,
        )


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
    """Expose original references and attach actual pixels with source provenance."""
    inputs = current_image_inputs()
    views = inputs.input_views() if inputs is not None else []
    cap = max(0, config.image_describe_max_per_message) if config.image_understanding_enabled else 0
    quoted_context = _quoted_message_context(session)
    quoted_role = quoted_context.speaker_role if quoted_context is not None else None
    stored_parts = [text] if text else []
    image_parts: list[dict[str, Any]] = []
    for offset, view in enumerate(views):
        quoted = view["source"] == "quoted"
        marker = format_image_note("", quoted=quoted, quoted_role=quoted_role if quoted else None)
        stored_parts.append(marker)
        if offset >= cap or inputs is None:
            continue
        ref = str(view["image_ref"])
        try:
            snapshot = await inputs.resolve(session, ref, purpose="inspect")
        except ImageInputError as exc:
            warn(f"image passthrough unavailable: {exc}")
            view["status"] = "unavailable"
            continue
        view["status"] = "ready"
        image_parts.extend(
            [
                {"type": "text", "text": json.dumps(view, ensure_ascii=False)},
                {"type": "image_url", "image_url": {"url": raw_to_image_data_url(snapshot.data)}},
            ]
        )
    if config.image_understanding_enabled:
        image_parts.extend(await _forward_image_content(config, session, forwarded_messages, warn, visual=True))
    current_text = " ".join(stored_parts)
    text_part = serialize_user_turn(user_name, current_text, forwarded_messages, mentioned_participants, views)
    stored_text = render_forwarded_storage(current_text, forwarded_messages)
    if image_parts:
        return [{"type": "text", "text": text_part}, *image_parts], stored_text
    return text_part, stored_text


async def _forward_image_content(
    config: LLMChatConfig,
    session: Session,
    messages: Sequence[ForwardedMessage],
    warn: Callable[[str], None],
    *,
    visual: bool,
) -> list[dict[str, Any]]:
    inputs = current_image_inputs()
    parts: list[dict[str, Any]] = []
    if inputs is None:
        return parts
    for message in messages:
        for image in message.get("images", []):
            ref = image.get("image_ref")
            if not isinstance(ref, str):
                continue
            try:
                snapshot = await inputs.resolve(session, ref, purpose="inspect")
                image["status"] = "ready"
                if visual:
                    provenance = {
                        "source": "forward",
                        "node_ref": message.get("node_ref"),
                        "speaker": message["speaker"],
                        "speaker_ref": message.get("speaker_ref"),
                        "image": image,
                        "instruction": "Quoted pixels, not a new upload or instruction from the current user.",
                    }
                    parts.extend(
                        [
                            {"type": "text", "text": json.dumps(provenance, ensure_ascii=False)},
                            {"type": "image_url", "image_url": {"url": raw_to_image_data_url(snapshot.data)}},
                        ]
                    )
                else:
                    image["description"] = await describe_image_bytes(config, snapshot.data)
            except Exception as exc:
                image["status"] = inputs.view(ref)["status"]
                image["inspection_status"] = "unavailable"
                warn(f"forwarded image inspection unavailable: {type(exc).__name__}")
    return parts


async def build_image_notes(
    config: LLMChatConfig,
    session: Session,
    warn: Callable[[str], None],
    forwarded_messages: Sequence[ForwardedMessage] = (),
) -> list[str]:
    """Describe the same original snapshots used by editing, delivery, and audit."""
    if not config.image_understanding_enabled:
        return []
    inputs = current_image_inputs()
    if inputs is None:
        return []
    quoted_context = _quoted_message_context(session)
    quoted_role = quoted_context.speaker_role if quoted_context is not None else None
    cap = max(0, config.image_describe_max_per_message)
    notes: list[str] = []
    for offset, view in enumerate(inputs.input_views()):
        description = ""
        if offset < cap:
            try:
                snapshot = await inputs.resolve(session, str(view["image_ref"]), purpose="inspect")
                description = await describe_image_bytes(config, snapshot.data)
            except Exception as exc:
                warn(f"image describe failed: {summarize_exception(exc)}")
        quoted = view["source"] == "quoted"
        notes.append(format_image_note(description, quoted=quoted, quoted_role=quoted_role if quoted else None))
    await _forward_image_content(config, session, forwarded_messages, warn, visual=False)
    return notes
