"""Optional local history preload for an already addressed ordinary chat turn."""

from __future__ import annotations

from collections.abc import Callable

from arclet.entari import Session, ChannelType

from .config import LLMChatConfig
from .perception import PerceptionProvider, get_channel_perception
from .core.channel_context import ChannelContextSnapshot, build_channel_context, unavailable_channel_context


async def read_channel_context(
    config: LLMChatConfig,
    session: Session,
    *,
    warn: Callable[[str], None],
    get_perception: PerceptionProvider = get_channel_perception,
) -> ChannelContextSnapshot | None:
    """Read before the real trigger; never widen a missing anchor to current history."""
    if not config.channel_context_enabled or session.channel.type == ChannelType.DIRECT:
        return None
    count = config.channel_context_messages
    message = getattr(session.event, "message", None)
    anchor = str(getattr(message, "id", "") or "")
    if not anchor:
        warn("channel context unavailable: missing trigger message")
        return unavailable_channel_context(count)
    try:
        messages, _ = await get_perception().recent_messages(session, limit=count, before_message_id=anchor)
        return build_channel_context(messages, requested_messages=count)
    except Exception as exc:
        warn(f"channel context unavailable: {type(exc).__name__}")
        return unavailable_channel_context(count)
