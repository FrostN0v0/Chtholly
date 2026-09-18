"""Model-controlled composition of exactly one native message chain per call."""

from __future__ import annotations

import re
from typing import Protocol
import asyncio
from dataclasses import dataclass
from collections.abc import Callable, Awaitable

from satori import (
    At,
    Bold,
    Code,
    File,
    Link,
    Text,
    Audio,
    Emoji,
    Image,
    Video,
    Italic,
    Element,
    Message,
    Spoiler,
    Underline,
    Strikethrough,
)
from pydantic import ValidationError
from arclet.entari import Session, MessageChain
from satori.element import Br
from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from ._delivery import send_with_delivery
from ..core.media import has_meaningful_text
from ..core.types import JSONType
from ..web.policy import WebAccessError, normalize_public_url
from ._registration import register_tool
from ..core.delivery import (
    DeliveryError,
    DeliveryRejected,
    reserve_message,
    clean_delivery_fragment,
    normalize_delivery_delay,
    require_llm_chat_delivery,
)
from ..prepared_media import PreparedMedia, confirm_media, consume_media, resolve_media
from ._message_models import (
    MESSAGE_SEGMENTS,
    LinkSegment,
    TextSegment,
    BreakSegment,
    EmojiSegment,
    MediaSegment,
    StyleSegment,
    MentionSegment,
    MessageSegment,
)
from ..core.media_delivery import current_media_requirements

_ONEBOT_PLATFORMS = frozenset({"onebot", "onebot11"})
_NATIVE_EMOJI_ID = re.compile(r"(?:0|[1-9][0-9]{0,9})\Z", re.ASCII)
_STYLES = {
    "bold": Bold,
    "italic": Italic,
    "underline": Underline,
    "strike": Strikethrough,
    "spoiler": Spoiler,
    "code": Code,
}
_MEDIA_MARKERS = {
    Image: "[image]",
    Audio: "[audio]",
    Video: "[video]",
    File: "[file]",
    Message: "[merged forward]",
}


class MentionParticipant(Protocol):
    platform_user_id: str
    display_name: str


MentionResolver = Callable[[Session, str], Awaitable[MentionParticipant | None]]


@dataclass(slots=True)
class SendMsgToolContext:
    """Runtime resolver for opaque current-channel mention targets."""

    resolve_participant: MentionResolver
    max_mentions: int = 3


def _clean_segments(segments: object, platform: str) -> list[MessageSegment]:
    """Validate the entire input before any resolver, reservation, or send."""

    try:
        parsed = MESSAGE_SEGMENTS.validate_python(segments, strict=True)
    except ValidationError:
        raise DeliveryRejected(
            "segments must be a list of valid tagged message segments with no extra fields"
        ) from None
    if not parsed:
        raise DeliveryRejected("segments must contain at least one message segment")
    cleaned: list[MessageSegment] = []
    pending_text: list[str] = []

    def flush_text() -> None:
        if pending_text:
            cleaned.append(TextSegment(type="text", text=clean_delivery_fragment("".join(pending_text), field="text")))
            pending_text.clear()

    for segment in parsed:
        if isinstance(segment, TextSegment):
            pending_text.append(segment.text)
            continue
        flush_text()
        if isinstance(segment, StyleSegment):
            segment = StyleSegment(
                type="style", style=segment.style, text=clean_delivery_fragment(segment.text, field="style.text")
            )
        elif isinstance(segment, LinkSegment):
            try:
                url = normalize_public_url(clean_delivery_fragment(segment.url, field="link.url"))
            except WebAccessError:
                raise DeliveryRejected("link.url must be a public HTTP(S) URL") from None
            segment = LinkSegment(type="link", url=url, text=clean_delivery_fragment(segment.text, field="link.text"))
        elif isinstance(segment, EmojiSegment):
            if platform not in _ONEBOT_PLATFORMS or _NATIVE_EMOJI_ID.fullmatch(segment.id) is None:
                raise DeliveryRejected("Native emoji requires a canonical numeric OneBot face ID of at most 10 digits")
        cleaned.append(segment)
    flush_text()
    authored = "".join(
        segment.text
        if isinstance(segment, (TextSegment, StyleSegment, LinkSegment))
        else "\n"
        if isinstance(segment, BreakSegment)
        else "\ufffc"
        for segment in cleaned
    )
    if clean_delivery_fragment(authored, field="segments") != authored:
        raise DeliveryRejected("Reserved control content cannot span message segment boundaries")
    return cleaned


def _validate_media(item: PreparedMedia, platform: str) -> None:
    element = item.element
    if type(element) not in _MEDIA_MARKERS:
        raise DeliveryRejected("Prepared media contains an unsupported native element")
    if type(element) is not Message:
        if element.children:
            raise DeliveryRejected("Prepared media must not contain nested native elements")
        return
    if platform not in _ONEBOT_PLATFORMS:
        raise DeliveryRejected("Merged forward delivery is unsupported on this platform")
    if (
        element.id is not None
        or element.forward is not True
        or not element.children
        or any(key not in {"id", "forward"} or value != getattr(element, key) for key, value in element._attrs.items())
    ):
        raise DeliveryRejected("Prepared merged forward has an unsafe native structure")
    for node in element.children:
        if (
            type(node) is not Message
            or node.id is not None
            or node.forward is not None
            or not node.children
            or any(key not in {"id", "forward"} or value != getattr(node, key) for key, value in node._attrs.items())
            or any(type(child) is not Text or child.children for child in node.children)
        ):
            raise DeliveryRejected("Prepared merged forward has an unsafe native structure")


def _display_name(value: object, user_id: str) -> str:
    """Never use platform identity as a visible fallback or historical mention."""

    text = str(value or "").strip()
    if not text or text == user_id or user_id in text:
        return "participant"
    try:
        cleaned = clean_delivery_fragment(text, field="mention display name")
        return cleaned if has_meaningful_text(cleaned) else "participant"
    except DeliveryError:
        return "participant"


async def _resolve_mentions(
    session: Session,
    segments: list[MessageSegment],
    runtime: SendMsgToolContext,
) -> dict[str, tuple[str, str]]:
    refs = [segment.target for segment in segments if isinstance(segment, MentionSegment)]
    if len(refs) > max(0, min(3, runtime.max_mentions)):
        raise DeliveryRejected("Message exceeds the configured mention occurrence limit")
    resolved: dict[str, tuple[str, str]] = {}
    for ref in refs:
        if ref in resolved:
            continue
        if ref == "current_user":
            user = getattr(session.event, "user", None)
            member = getattr(session.event, "member", None)
            user_id = str(getattr(user, "id", "") or "").strip()
            name = getattr(member, "nick", "") or getattr(user, "name", "") or getattr(user, "nick", "")
            is_bot = getattr(user, "is_bot", False)
        else:
            participant = await runtime.resolve_participant(session, ref)
            if participant is None:
                raise DeliveryRejected("Mention target is unavailable in the current channel")
            user_id = str(participant.platform_user_id or "").strip()
            name = participant.display_name
            is_bot = getattr(participant, "is_bot", False)
        if not user_id:
            raise DeliveryRejected("Mention target has no deliverable platform identity")
        if is_bot or user_id == str(session.account.self_id):
            raise DeliveryRejected("Mention target cannot be a bot")
        resolved[ref] = (user_id, _display_name(name, user_id))
    return resolved


def _compose(
    segments: list[MessageSegment],
    mentions: dict[str, tuple[str, str]],
    media: tuple[PreparedMedia, ...],
    platform: str,
) -> tuple[MessageChain, str]:
    elements: list[Element] = []
    visible: list[str] = []
    prepared = iter(media)
    for segment in segments:
        if isinstance(segment, TextSegment):
            elements.append(Text(segment.text))
            visible.append(segment.text)
        elif isinstance(segment, MentionSegment):
            user_id, name = mentions[segment.target]
            elements.append(At(user_id, name=name))
            visible.append(f"@{name}")
        elif isinstance(segment, LinkSegment):
            elements.append(Link(segment.url)(Text(segment.text)))
            visible.append(
                f"{segment.text} ({segment.url})" if platform in _ONEBOT_PLATFORMS else segment.text or segment.url
            )
        elif isinstance(segment, EmojiSegment):
            elements.append(Emoji(segment.id))
            visible.append("[native emoji]")
        elif isinstance(segment, MediaSegment):
            item = next(prepared)
            elements.append(item.element)
            visible.append(item.history_marker or _MEDIA_MARKERS[type(item.element)])
        elif isinstance(segment, BreakSegment):
            elements.append(Br())
            visible.append("\n")
        elif isinstance(segment, StyleSegment):
            elements.append(_STYLES[segment.style](Text(segment.text)))
            visible.append(segment.text)
    return MessageChain(elements), "".join(visible)


def register_send_msg(
    dispatcher: PluginDispatcher[JSONType],
    runtime: SendMsgToolContext,
) -> Subscriber[JSONType]:
    """Register the sole model-controlled native message delivery tool."""

    async def send_msg(
        session: Session,
        segments: list[
            TextSegment | MentionSegment | LinkSegment | EmojiSegment | MediaSegment | BreakSegment | StyleSegment
        ],
        delay_seconds: float | None = None,
    ) -> JSONType:
        """Send exactly one message chain, in the exact segment order and with no inserted spacing.

        Each call sends one conversational beat, not the whole turn. Call again for the next complete part when
        answer, reason, advice, or a follow-up reaction naturally belong in separate messages. Keep a short answer
        in one call; do not split every sentence or add filler. Newlines inside one call do not create new messages.
        Text is literal, never Satori markup. Place mentions exactly where direct address belongs; use current_user
        or an opaque current-channel participant_ref, never a raw ID.
        At most three mention occurrences are allowed, including repeats. Obtain media_ref from a preparation tool
        first, then place media segments anywhere in this message. References expire with this generation and cannot
        be reused after a send attempt. On OneBot, audio, video, file, and merged forward must each be the only segment.
        A required source edit or web-reference image must be included before any other message may be sent.
        Links require public HTTP(S) URLs; OneBot emoji IDs must be numeric. Unsupported composites are rejected,
        never split or retried through another delivery path. After success, do not repeat this content in final text.

        Args:
            segments: Tagged text, mention, link, emoji, media, break, or style segments, in exact visible order.
            delay_seconds: Target interval from the preceding confirmed or possibly confirmed delivery.
        Returns:
            A privacy-safe confirmation and guidance, without message content or capability references.
        """

        require_llm_chat_delivery()
        platform = str(session.account.platform).casefold()
        parsed = _clean_segments(segments, platform)
        delay = normalize_delivery_delay(delay_seconds)
        refs = [segment.media_ref for segment in parsed if isinstance(segment, MediaSegment)]
        media = resolve_media(session, refs) if refs else ()
        for item in media:
            _validate_media(item, platform)
        if (
            platform in _ONEBOT_PLATFORMS
            and len(parsed) != 1
            and any(type(item.element) in {Audio, Video, File, Message} for item in media)
        ):
            raise DeliveryRejected("OneBot audio, video, file, and merged forward require a standalone media message")
        requirements = current_media_requirements()
        if (
            requirements is not None
            and requirements.intent.requires_provenance
            and not requirements.confirmed
            and not any(requirements.accepts(item.provenance) for item in media)
        ):
            raise DeliveryRejected("This message must include an image satisfying the requested source and references")
        try:
            mentions = await _resolve_mentions(session, parsed, runtime)
        except asyncio.CancelledError:
            raise
        except DeliveryError:
            raise
        except Exception as exc:
            raise DeliveryError(f"send_msg mention resolution failed: {type(exc).__name__}") from None
        payload, projection = _compose(parsed, mentions, media, platform)
        text_message = any(not isinstance(segment, (MediaSegment, BreakSegment)) for segment in parsed)
        state = reserve_message(
            projection,
            media_count=len(media),
            text_message=text_message,
        )
        if media:
            consume_media(media)
        try:
            await send_with_delivery(
                session,
                payload,
                state,
                delay_seconds=delay,
                texts=[projection],
                media=len(media),
                text_message=text_message,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise DeliveryError(f"send_msg delivery outcome is unknown: {type(exc).__name__}; do not resend") from None
        if media:
            await confirm_media(media)
        return {
            "status": "delivered",
            "messages": 1,
            "media_count": len(media),
            "guidance": (
                "This message is confirmed. Use send_msg again for the next natural reply beat if needed; "
                "do not repeat this content. End only when all intended parts have been delivered."
            ),
        }

    return register_tool(dispatcher, send_msg)


__all__ = ["SendMsgToolContext", "register_send_msg"]
