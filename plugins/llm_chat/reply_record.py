"""Join native replies to bounded channel records without issuing capabilities."""

from __future__ import annotations

from collections.abc import Callable

from arclet.entari import Session, ChannelType

from .perception import PerceptionProvider, get_channel_perception
from .reply_context import ReplyContext


async def bind_reply_record(
    session: Session,
    reply: ReplyContext | None,
    *,
    warn: Callable[[str], None],
    get_perception: PerceptionProvider = get_channel_perception,
) -> ReplyContext | None:
    """Preserve native content; recover missing text only from the exact readable record."""
    if reply is None:
        return None
    bound = reply.copy()
    bound["record_status"] = "unavailable"
    if session.channel.type == ChannelType.DIRECT:
        return bound
    try:
        record = await get_perception().replied_message(session)
    except Exception as exc:
        warn(f"reply record unavailable: {type(exc).__name__}")
        return bound
    if record is None or not isinstance(record.get("is_bot"), bool):
        return bound
    bound["record_status"] = "matched"
    bound["speaker_role"] = "assistant" if record["is_bot"] else "participant"
    speaker = record.get("display_name")
    bound["speaker"] = (
        "bot" if record["is_bot"] else speaker if isinstance(speaker, str) and speaker else "Unknown sender"
    )
    content = record.get("content")
    if bound["content_status"] == "unavailable" and isinstance(content, str) and content.strip():
        bound["content"] = content
        bound["content_status"] = "recorded"
    return bound
