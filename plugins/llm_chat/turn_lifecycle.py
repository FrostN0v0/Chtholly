"""Persistence and final-delivery lifecycle for one claimed llm_chat turn."""

from __future__ import annotations

from typing import Protocol
import asyncio
from dataclasses import field, dataclass
from collections.abc import Callable, Sequence, Awaitable

from arclet.entari import Session

from .core.media import strip_internal_media_records
from .core.delivery import (
    DeliveryError,
    DeliveryState,
    render_delivered_text,
    reserve_final_text_messages,
    strip_trailing_end_of_response,
)
from .prepared_media import ensure_media_capacity
from .core.tool_trace import ToolTraceRecorder
from .tools._delivery import send_with_delivery
from .core.agent_trace import AgentEventDraft, AgentTurnRecorder
from .tools._rendering import prepare_image_bytes
from .core.image_source import IMAGE_FETCH_MAX_BYTES
from .core.native_images import extract_native_images
from .core.media_delivery import strip_media_unavailable_marker
from .tools.prepare_external_media import fetch_public_media

HistoryAppender = Callable[[str, str, str, str, str], Awaitable[object]]
HistoryDeleter = Callable[[int], Awaitable[object]]
WarningSink = Callable[[str], object]


AgentEventPersister = Callable[[int, Sequence[AgentEventDraft]], Awaitable[object]]


class AgentTurnFinisher(Protocol):
    def __call__(self, turn_id: int, *, status: str, final_text: str) -> Awaitable[object]: ...


@dataclass
class ActiveChatTurn:
    """Own one persisted user turn and its confirmed assistant deliveries."""

    channel_id: str
    user_message_id: int
    delivery_state: DeliveryState
    append_history: HistoryAppender
    delete_history: HistoryDeleter
    warn: WarningSink
    persist_agent_event_rows: AgentEventPersister | None = None
    finish_agent_turn_row: AgentTurnFinisher | None = None
    tool_trace: ToolTraceRecorder = field(default_factory=ToolTraceRecorder)
    agent_turn_id: int | None = None
    agent_events: AgentTurnRecorder = field(default_factory=AgentTurnRecorder)
    _tool_events_recorded: bool = field(default=False, init=False)
    _agent_finalize_attempted: bool = field(default=False, init=False)
    _assistant_persist_attempted: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self.agent_events.warn = self.warn
        if self.agent_turn_id is not None and self.persist_agent_event_rows is not None:

            async def persist(events: Sequence[AgentEventDraft]) -> object:
                assert self.agent_turn_id is not None
                assert self.persist_agent_event_rows is not None
                return await self.persist_agent_event_rows(self.agent_turn_id, events)

            self.agent_events.sink = persist

    async def persist_delivered_text(self, *, preserve_original: bool = False) -> str:
        """Persist confirmed text deliveries at most once."""

        delivered_text = render_delivered_text(self.delivery_state)
        if self._assistant_persist_attempted or not delivered_text:
            return delivered_text
        self._assistant_persist_attempted = True
        self.agent_events.record_assistant_output(delivered_text)
        try:
            await self.append_history(self.channel_id, "", "bot", "assistant", delivered_text)
        except asyncio.CancelledError:
            self.warn("assistant delivery persistence cancelled")
            if not preserve_original:
                raise
        except Exception as exc:
            self.warn(f"assistant delivery persistence failed: {type(exc).__name__}")
        return delivered_text

    def capture_tool_events(self) -> None:
        if self._tool_events_recorded:
            return
        self._tool_events_recorded = True
        self.agent_events.record_tool_events(self.tool_trace.events)

    async def finalize_agent_turn(self, status: str) -> None:
        """Persist AgentEvent rows and terminal turn state exactly once."""

        if (
            self._agent_finalize_attempted
            or self.agent_turn_id is None
            or self.persist_agent_event_rows is None
            or self.finish_agent_turn_row is None
        ):
            return
        self._agent_finalize_attempted = True
        self.capture_tool_events()
        persisted = await self.agent_events.flush()
        try:
            await self.finish_agent_turn_row(
                self.agent_turn_id,
                status=status if persisted else "event_persistence_failed",
                final_text=render_delivered_text(self.delivery_state),
            )
        except asyncio.CancelledError:
            self.warn("agent turn finalization cancelled")
            raise
        except Exception as exc:
            self.warn(f"agent turn finalization failed: {type(exc).__name__}")

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

    async def prepare_model_images(self, session: Session, response: object) -> bool:
        """Prepare native model output for send_msg without claiming delivery."""
        images = extract_native_images(response)
        if not images:
            return True
        data: list[bytes] = []
        for image in images:
            if image.content is not None:
                data.append(image.content)
            elif image.url is not None:
                raw = await fetch_public_media(image.url, max_bytes=IMAGE_FETCH_MAX_BYTES)
                data.append(raw)
            else:
                raise DeliveryError("Native image has no usable content")
        ensure_media_capacity(len(data), byte_count=sum(map(len, data)))
        for raw in data:
            await prepare_image_bytes(session, raw, warn=self.warn, tool_name="native_image")
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
                    await send_with_delivery(session, final_reply, self.delivery_state, texts=[final_reply])
                except BaseException:
                    await self.preserve_and_rollback()
                    raise
        return True
