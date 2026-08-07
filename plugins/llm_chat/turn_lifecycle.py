"""Persistence and final-delivery lifecycle for one claimed llm_chat turn."""

from __future__ import annotations

import asyncio
from dataclasses import field, dataclass
from collections.abc import Callable, Awaitable

from arclet.entari import Session

from .core.media import strip_internal_media_records
from .core.delivery import (
    DeliveryError,
    DeliveryState,
    wait_for_delivery,
    mark_delivery_attempt,
    mark_delivery_success,
    render_delivered_text,
    reserve_final_text_messages,
    strip_trailing_end_of_response,
)
from .core.media_delivery import strip_media_unavailable_marker

HistoryAppender = Callable[[str, str, str, str, str], Awaitable[object]]
HistoryDeleter = Callable[[int], Awaitable[object]]
WarningSink = Callable[[str], object]


@dataclass
class ActiveChatTurn:
    """Own one persisted user turn and its confirmed assistant deliveries."""

    channel_id: str
    user_message_id: int
    delivery_state: DeliveryState
    append_history: HistoryAppender
    delete_history: HistoryDeleter
    warn: WarningSink
    _assistant_persist_attempted: bool = field(default=False, init=False)

    async def persist_delivered_text(self, *, preserve_original: bool = False) -> str:
        """Persist confirmed text deliveries at most once."""

        delivered_text = render_delivered_text(self.delivery_state)
        if self._assistant_persist_attempted or not delivered_text:
            return delivered_text
        self._assistant_persist_attempted = True
        try:
            await self.append_history(self.channel_id, "", "bot", "assistant", delivered_text)
        except asyncio.CancelledError:
            self.warn("assistant delivery persistence cancelled")
            if not preserve_original:
                raise
        except Exception as exc:
            self.warn(f"assistant delivery persistence failed: {type(exc).__name__}")
        return delivered_text

    async def rollback_if_unstarted(self) -> None:
        """Delete the user history row only when no delivery attempt occurred."""

        if self.delivery_state.delivery_attempts:
            return
        try:
            await self.delete_history(self.user_message_id)
        except asyncio.CancelledError:
            self.warn("user turn rollback cancelled")
        except Exception as exc:
            self.warn(f"user turn rollback failed: {type(exc).__name__}")

    async def preserve_and_rollback(self) -> None:
        """Preserve any confirmed prefix and remove an unstarted user turn."""

        await self.persist_delivered_text(preserve_original=True)
        await self.rollback_if_unstarted()

    async def deliver_model_reply(self, session: Session, raw_reply: str) -> bool:
        """Sanitize and deliver final model text after any tool-delivered prefix."""

        stripped_raw_reply = raw_reply.strip()
        reply_without_media = strip_internal_media_records(raw_reply).strip()
        if reply_without_media != stripped_raw_reply:
            self.warn("stripped reserved media history marker from model reply")
        reply_without_control = strip_media_unavailable_marker(reply_without_media).strip()
        if reply_without_control != reply_without_media:
            self.warn("stripped media-unavailable control marker from model reply")
        reply = strip_trailing_end_of_response(reply_without_control)
        if reply != reply_without_control:
            self.warn("stripped trailing end-of-response marker from model reply")
        if not reply and self.delivery_state.confirmed_deliveries == 0:
            await self.rollback_if_unstarted()
            self.warn("model reply produced no confirmed delivery")
            return False

        if reply:
            try:
                final_replies = reserve_final_text_messages(self.delivery_state, reply)
            except DeliveryError:
                self.warn("suppressed final supplement outside delivery budget")
                final_replies = ()
            if not final_replies and self.delivery_state.confirmed_deliveries == 0:
                await self.rollback_if_unstarted()
                self.warn("final reply was suppressed without confirmed delivery")
                return False
            for final_reply in final_replies:
                try:
                    await wait_for_delivery(self.delivery_state)
                except asyncio.CancelledError:
                    await self.preserve_and_rollback()
                    raise
                except Exception:
                    await self.preserve_and_rollback()
                    raise
                try:
                    await session.send(final_reply)
                except asyncio.CancelledError:
                    mark_delivery_attempt(self.delivery_state)
                    await self.persist_delivered_text(preserve_original=True)
                    raise
                except Exception:
                    mark_delivery_attempt(self.delivery_state)
                    await self.persist_delivered_text(preserve_original=True)
                    raise
                mark_delivery_success(self.delivery_state, [final_reply])
        return True
