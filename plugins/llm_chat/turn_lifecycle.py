"""Persistence and final-delivery lifecycle for one claimed llm_chat turn."""

from __future__ import annotations

import asyncio
from dataclasses import field, dataclass
from collections.abc import Callable, Sequence, Awaitable

from arclet.entari import Session, MessageChain

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
    reserve_media_messages_for_state,
)
from .core.tool_trace import ToolTraceEvent, ToolTraceRecorder
from .tools._delivery import send_with_delivery
from .core.native_images import to_entari_image, extract_native_images
from .core.media_delivery import strip_media_unavailable_marker

HistoryAppender = Callable[[str, str, str, str, str], Awaitable[object]]
HistoryDeleter = Callable[[int], Awaitable[object]]
ToolTracePersister = Callable[[str, int, Sequence[ToolTraceEvent], int], Awaitable[object]]
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
    tool_trace: ToolTraceRecorder = field(default_factory=ToolTraceRecorder)
    persist_tool_events: ToolTracePersister | None = None
    tool_history_retention: int = 200
    _tool_trace_persist_attempted: bool = field(default=False, init=False)
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

    async def persist_tool_trace(self) -> None:
        """Persist completed tool events at most once without blocking delivery."""

        if self._tool_trace_persist_attempted or not self.tool_trace.events or self.persist_tool_events is None:
            return
        self._tool_trace_persist_attempted = True
        try:
            await self.persist_tool_events(
                self.channel_id,
                self.user_message_id,
                tuple(sorted(self.tool_trace.events, key=lambda event: event.sequence)),
                self.tool_history_retention,
            )
        except asyncio.CancelledError:
            self.warn("tool trace persistence cancelled")
            raise
        except Exception as exc:
            self.warn(f"tool trace persistence failed: {type(exc).__name__}")

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

    async def deliver_model_images(self, session: Session, response: object) -> bool:
        """Deliver safe native model images before any final text."""

        images = extract_native_images(response)
        if not images:
            return True
        try:
            reserve_media_messages_for_state(self.delivery_state, len(images))
        except DeliveryError:
            await self.preserve_and_rollback()
            raise
        total = len(images)
        for index, image in enumerate(images):
            try:
                payload = MessageChain([to_entari_image(image)])
                await send_with_delivery(session, payload, self.delivery_state, media=True)
            except asyncio.CancelledError:
                await self.persist_delivered_text(preserve_original=True)
                raise
            except Exception:
                await self.persist_delivered_text(preserve_original=True)
                if not self.delivery_state.delivery_attempts:
                    await self.rollback_if_unstarted()
                if index:
                    raise DeliveryError(
                        f"native image delivery confirmed {index}/{total} images before failure; "
                        "do not repeat the confirmed prefix"
                    ) from None
                raise
            try:
                await self.append_history(self.channel_id, "", "bot", "assistant", "[发送了图片]")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.warn(f"native image delivery history failed: {type(exc).__name__}")
        return True

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
