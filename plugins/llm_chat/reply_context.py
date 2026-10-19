"""Host-derived ordinary reply attribution, separate from merged forwards."""

from __future__ import annotations

from typing import Literal, TypedDict, NotRequired
from dataclasses import dataclass

from arclet.entari import Image, Author, Session, MessageChain

from .core.forward import ForwardedSpeakerRole


class ReplyContext(TypedDict):
    speaker: str
    speaker_role: ForwardedSpeakerRole
    content: str
    content_status: Literal["available", "unavailable", "recorded"]
    record_status: NotRequired[Literal["matched", "unavailable"]]
    images: NotRequired[list[dict[str, object]]]


@dataclass(frozen=True, slots=True)
class _ReplySource:
    elements: MessageChain
    speaker: str
    speaker_role: ForwardedSpeakerRole


def _native_id(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _reply_source(session: Session) -> _ReplySource | None:
    reply = getattr(session, "reply", None)
    quote = getattr(reply, "quote", None) if reply is not None else None
    quote = quote if quote is not None else getattr(session, "quote", None)
    if reply is None and (quote is None or not (getattr(quote, "id", None) or getattr(quote, "children", None))):
        return None
    origin = getattr(reply, "origin", None)
    body = getattr(origin, "message", None)
    elements = MessageChain(body or getattr(quote, "children", ()) or ())
    user = getattr(origin, "user", None)
    member = getattr(origin, "member", None)
    author = next((element for element in elements if isinstance(element, Author)), None)
    if author is None and quote is not None:
        author = next(
            (element for element in getattr(quote, "children", ()) or () if isinstance(element, Author)), None
        )
    account = getattr(session, "account", None)
    self_user = getattr(getattr(account, "self_info", None), "user", None)
    self_ids = {
        value
        for value in (_native_id(getattr(account, "self_id", None)), _native_id(getattr(self_user, "id", None)))
        if value
    }
    # A hydrated native sender is authoritative even if a quote's Author disagrees.
    sender_id = _native_id(getattr(user, "id", None)) or _native_id(getattr(author, "id", None))
    if sender_id and sender_id in self_ids:
        return _ReplySource(elements, "bot", "assistant")
    name = (
        getattr(member, "nick", None)
        or getattr(user, "nick", None)
        or getattr(user, "name", None)
        or getattr(author, "name", None)
    )
    # Display names are not identities and can never prove that a sender is self.
    role: ForwardedSpeakerRole = "participant" if sender_id and self_ids else "unknown"
    return _ReplySource(elements, str(name) if name else "Unknown sender", role)


def collect_reply_context(session: Session) -> ReplyContext | None:
    """Collect text/image-only and unavailable native replies without exposing IDs."""
    source = _reply_source(session)
    if source is None:
        return None
    parts = [source.elements.extract_plain_text().strip()]
    parts.extend("[Image]" for element in source.elements if isinstance(element, Image))
    content = " ".join(part for part in parts if part)
    return {
        "speaker": source.speaker,
        "speaker_role": source.speaker_role,
        "content": content or "[Reply content unavailable]",
        "content_status": "available" if content else "unavailable",
    }


def collect_reply_images(session: Session) -> list[Image]:
    """Use the same hydrated body and author resolution as the reply collector."""
    source = _reply_source(session)
    return [element for element in source.elements if isinstance(element, Image)] if source is not None else []
