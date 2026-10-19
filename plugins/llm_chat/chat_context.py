"""Import-light helpers for chat message assembly."""

from __future__ import annotations

import json
from typing import Any
from collections.abc import Callable, Sequence

import litellm
from arclet.entari import Image, Session, MessageChain

from .config import LLMChatConfig
from .models import Conversation
from .vision import describe_image_bytes
from .core.media import format_image_note, sanitize_assistant_history
from .perception import MentionedParticipant
from .core.forward import ForwardedMessage, forwarded_storage_views
from .image_inputs import ImageInputs, ImageInputError, current_image_inputs
from .reply_context import ReplyContext, collect_reply_images, collect_reply_context
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
    *,
    reply_to: ReplyContext | None = None,
    durable: bool = False,
) -> str:
    """Serialize one user turn as unambiguous structured JSON data."""
    payload: dict[str, object] = {"speaker": user_name, "content": content}
    if reply_to is not None:
        payload["reply_to"] = _durable_reply(reply_to) if durable else reply_to
    if input_images:
        payload["input_images"] = _durable_image_views(input_images) if durable else list(input_images)
    if mentioned_participants:
        payload["mentioned_participants"] = list(mentioned_participants)
    if forwarded_messages:
        payload["forwarded_messages"] = (
            forwarded_storage_views(forwarded_messages) if durable else list(forwarded_messages)
        )
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _durable_image_views(images: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    return [{key: value for key, value in image.items() if key != "image_ref"} for image in images]


def _durable_reply(reply_to: ReplyContext) -> ReplyContext:
    reply: ReplyContext = {**reply_to}
    if "images" in reply:
        reply["images"] = _durable_image_views(reply["images"])
    return reply


def render_user_storage(
    content: str,
    forwarded_messages: Sequence[ForwardedMessage] = (),
    *,
    reply_to: ReplyContext | None = None,
    input_images: Sequence[dict[str, object]] = (),
) -> str:
    """Store durable attribution without generation-local image capabilities."""
    if not forwarded_messages and reply_to is None and not input_images:
        return content
    payload: dict[str, object] = {"content": content}
    if forwarded_messages:
        payload["forwarded_messages"] = forwarded_storage_views(forwarded_messages)
    if reply_to is not None:
        payload["reply_to"] = _durable_reply(reply_to)
    if input_images:
        payload["input_images"] = _durable_image_views(input_images)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def build_chat_messages(
    history: Sequence[Conversation],
    user_name: str,
    content: str,
    current_content: str | list[dict[str, Any]] | None = None,
    current_forwarded_messages: Sequence[ForwardedMessage] = (),
    current_mentioned_participants: Sequence[MentionedParticipant] = (),
    *,
    current_reply_to: ReplyContext | None = None,
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
                    reply_to=current_reply_to,
                )
            ),
        }
    )
    return messages


def collect_top_level_images(elements: MessageChain) -> list[Image]:
    """Collect images that are direct children of one message chain."""
    return [element for element in elements if isinstance(element, Image)]


def collect_message_images(session: Session) -> list[tuple[Image, bool]]:
    """Collect direct images first, then quoted images."""
    direct = collect_top_level_images(session.elements)
    quoted = collect_reply_images(session)
    return [(img, False) for img in direct] + [(img, True) for img in quoted]


def register_message_image_inputs(
    session: Session, inputs: ImageInputs, *, reply_to: ReplyContext | None = None
) -> None:
    """Record every direct/quoted position before any image acquisition or perception."""
    reply = reply_to if reply_to is not None else collect_reply_context(session)
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
            speaker_role=reply["speaker_role"]
            if quoted and reply is not None
            else "unknown"
            if quoted
            else "current_user",
            message_context="reply_to" if quoted else "current_input",
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
    *,
    reply_to: ReplyContext | None = None,
) -> tuple[list[dict[str, Any]] | str, str]:
    """Expose original references and attach actual pixels with source provenance."""
    inputs = current_image_inputs()
    views = inputs.input_views(session=session) if inputs is not None else []
    cap = max(0, config.image_describe_max_per_message) if config.image_understanding_enabled else 0
    image_parts: list[dict[str, Any]] = []
    for offset, view in enumerate(views):
        ref = str(view["image_ref"])
        if offset >= cap or inputs is None:
            continue
        try:
            snapshot = await inputs.resolve(session, ref, purpose="inspect")
        except ImageInputError as exc:
            warn(f"image passthrough unavailable: {exc}")
            continue
        inputs.mark_observed(ref, pixels=True)
        image_parts.extend(
            [
                {"type": "text", "text": json.dumps(inputs.view(ref), ensure_ascii=False)},
                {"type": "image_url", "image_url": {"url": raw_to_image_data_url(snapshot.data)}},
            ]
        )
    if config.image_understanding_enabled:
        image_parts.extend(await _forward_image_content(config, session, forwarded_messages, warn, visual=True))
    views = inputs.input_views(session=session) if inputs is not None else []
    reply = reply_with_image_views(reply_to if reply_to is not None else collect_reply_context(session), views)
    current_text = " ".join(part for part in [text, *direct_image_notes(views)] if part)
    text_part = serialize_user_turn(
        user_name, current_text, forwarded_messages, mentioned_participants, views, reply_to=reply
    )
    stored_text = render_user_storage(current_text, forwarded_messages, reply_to=reply, input_images=views)
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
                    inputs.mark_observed(ref, pixels=True)
                image.update(inputs.view(ref))
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
                    inputs.mark_observed(ref, description=str(image["description"]))
                    image.update(inputs.view(ref))
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
    inputs = current_image_inputs()
    if inputs is None:
        return []
    cap = max(0, config.image_describe_max_per_message) if config.image_understanding_enabled else 0
    for offset, view in enumerate(inputs.input_views(session=session)):
        if offset < cap:
            try:
                ref = str(view["image_ref"])
                snapshot = await inputs.resolve(session, ref, purpose="inspect")
                description = await describe_image_bytes(config, snapshot.data)
                inputs.mark_observed(ref, description=description)
            except Exception as exc:
                warn(f"image describe failed: {type(exc).__name__}")
    if config.image_understanding_enabled:
        await _forward_image_content(config, session, forwarded_messages, warn, visual=False)
    return direct_image_notes(inputs.input_views(session=session))


def direct_image_notes(views: Sequence[dict[str, object]]) -> list[str]:
    """Keep quoted media out of the current user's own message body."""
    return [
        format_image_note(
            str(view.get("description", "")),
            status=str(view["status"]),
            observation=str(view.get("observation", "unobserved")),
        )
        for view in views
        if view["source"] == "direct"
    ]


def reply_with_image_views(reply_to: ReplyContext | None, views: Sequence[dict[str, object]]) -> ReplyContext | None:
    """Attach only this input's quoted positions to its ordinary reply context."""
    if reply_to is None:
        return None
    reply = ReplyContext(**reply_to)
    images = [view for view in views if view["source"] == "quoted"]
    if images:
        reply["images"] = images
    return reply
