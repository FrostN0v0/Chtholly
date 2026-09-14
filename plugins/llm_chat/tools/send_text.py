"""send_text LLM tool implementation."""

from __future__ import annotations

import re
from typing import Protocol, cast
import asyncio
from dataclasses import dataclass
from collections.abc import Callable, Sequence, Awaitable

from arclet.entari import At, Text, Session, MessageChain
from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from ._delivery import send_with_delivery
from ..core.types import JSONType
from ._registration import register_tool
from ..core.delivery import (
    DeliveryError,
    reserve_text_message,
    normalize_delivery_text,
    normalize_delivery_delay,
)

CURRENT_USER_MENTION = "current_user"
MAX_MENTIONS_PER_MESSAGE = 3
_PARTICIPANT_REF = re.compile(r"participant_[0-9a-f]{10}", re.IGNORECASE)


class MentionParticipant(Protocol):
    platform_user_id: str
    display_name: str


MentionResolver = Callable[[Session, str], Awaitable[MentionParticipant | None]]


@dataclass(slots=True)
class SendTextToolContext:
    """Runtime resolver for current-channel mention targets."""

    resolve_participant: MentionResolver
    max_mentions: int = MAX_MENTIONS_PER_MESSAGE


@dataclass(frozen=True, slots=True)
class ResolvedMention:
    user_id: str
    display_name: str


def normalize_mention_refs(value: object, *, max_mentions: int) -> tuple[str, ...]:
    """Validate opaque mention targets without accepting raw platform IDs."""

    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise DeliveryError("mentions must be a list of current_user or participant_ref strings")
    if len(value) > max_mentions:
        raise DeliveryError(f"mentions exceeds the configured per-message limit ({max_mentions})")
    normalized: list[str] = []
    for item in value:
        target = item.strip().casefold()
        if target != CURRENT_USER_MENTION and _PARTICIPANT_REF.fullmatch(target) is None:
            raise DeliveryError("mentions contains an invalid current-channel target")
        if target not in normalized:
            normalized.append(target)
    return tuple(normalized)


def _current_user_mention(session: Session) -> ResolvedMention:
    event = session.event
    user = getattr(event, "user", None)
    user_id = str(getattr(user, "id", "") or "").strip()
    if not user_id:
        raise DeliveryError("current user is unavailable for mention")
    member = getattr(event, "member", None)
    display_name = str(getattr(member, "nick", "") or getattr(user, "name", "") or user_id).strip()
    return ResolvedMention(user_id, display_name)


async def resolve_mentions(
    session: Session,
    refs: Sequence[str],
    runtime: SendTextToolContext,
) -> tuple[ResolvedMention, ...]:
    """Resolve only current-user or current-channel opaque references."""

    resolved: list[ResolvedMention] = []
    seen_user_ids: set[str] = set()
    for ref in refs:
        if ref == CURRENT_USER_MENTION:
            mention = _current_user_mention(session)
        else:
            participant = await runtime.resolve_participant(session, ref)
            if participant is None:
                raise DeliveryError("mention target is unavailable in the current channel")
            user_id = str(participant.platform_user_id).strip()
            if not user_id:
                raise DeliveryError("mention target has no deliverable platform identity")
            display_name = str(participant.display_name or user_id).strip()
            mention = ResolvedMention(user_id, display_name)
        if mention.user_id == session.account.self_id:
            raise DeliveryError("mention target cannot be the current bot")
        if mention.user_id not in seen_user_ids:
            seen_user_ids.add(mention.user_id)
            resolved.append(mention)
    return tuple(resolved)


def build_mentioned_text_payload(text: str, mentions: Sequence[ResolvedMention]) -> str | MessageChain:
    """Prefix one text bubble with real Satori mention elements."""

    if not mentions:
        return text
    elements: list[At | Text] = []
    for mention in mentions:
        elements.extend((At(mention.user_id, name=mention.display_name), Text(" ")))
    elements.append(Text(text))
    return MessageChain(elements)


def _history_text(text: str, mentions: Sequence[ResolvedMention]) -> str:
    if not mentions:
        return text
    prefix = " ".join(f"@{mention.display_name}" for mention in mentions)
    return f"{prefix} {text}"


def register_send_text(
    dispatcher: PluginDispatcher[JSONType],
    runtime: SendTextToolContext,
) -> Subscriber[JSONType]:
    """Register paced text delivery with bounded current-channel mentions."""

    async def send_text(
        session: Session,
        text: str,
        delay_seconds: float | None = None,
        mentions: list[str] = cast(list[str], None),
    ) -> str:
        """Send one paced text bubble, optionally mentioning current-channel participants.

        Prefer this when a reply has two or more naturally separate chat beats, including factual answers with a
        conclusion followed by a reason, caveat, or follow-up. Use this whenever a real mention is needed, including
        for one short bubble; final response text cannot create a platform mention. Mentions are optional and should
        be used only for deliberate direct address, summoning, handoff, or multi-person disambiguation, never on every
        reply. Use current_user for the current speaker. For anyone else, first resolve one unambiguous participant_ref
        with find_channel_participants or current-channel context. Never place raw platform IDs, participant_ref values,
        or a textual @name inside text.

        Args:
            text (str): One complete visible text segment without internal control markers or textual fake mentions.
            delay_seconds (float | None): Target interval from the previous confirmed or possibly confirmed delivery.
            mentions (list[str] | None): Up to 3 targets: current_user or exact current-channel participant_ref values.
        Returns:
            str: Delivery result and final-response guidance without exposing resolved identities.
        """

        delay = normalize_delivery_delay(delay_seconds)
        normalized_text = normalize_delivery_text(text, field="text")
        mention_refs = normalize_mention_refs(mentions, max_mentions=max(0, runtime.max_mentions))
        try:
            resolved_mentions = await resolve_mentions(session, mention_refs, runtime)
        except asyncio.CancelledError:
            raise
        except DeliveryError:
            raise
        except Exception as exc:
            raise DeliveryError(f"send_text mention resolution failed: {type(exc).__name__}") from None

        delivery_state, normalized_text = reserve_text_message(normalized_text)
        payload = build_mentioned_text_payload(normalized_text, resolved_mentions)
        delivered_text = _history_text(normalized_text, resolved_mentions)
        try:
            await send_with_delivery(
                session,
                payload,
                delivery_state,
                delay_seconds=delay,
                texts=[delivered_text],
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise DeliveryError(f"send_text delivery failed: {type(exc).__name__}") from None
        mention_note = f"，已艾特 {len(resolved_mentions)} 人" if resolved_mentions else ""
        return f"已发送 1 条文本消息{mention_note}；不要在最终回复中重复，若无需补充只返回 [END_OF_RESPONSE]。"

    return register_tool(dispatcher, send_text)


__all__ = [
    "CURRENT_USER_MENTION",
    "MAX_MENTIONS_PER_MESSAGE",
    "ResolvedMention",
    "SendTextToolContext",
    "build_mentioned_text_payload",
    "normalize_mention_refs",
    "register_send_text",
    "resolve_mentions",
]
