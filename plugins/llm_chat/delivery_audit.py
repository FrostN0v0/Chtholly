"""Generation-local, operator-only receipts from Entari's actual send boundary."""

from __future__ import annotations

import time
import asyncio
from pathlib import Path
from datetime import datetime, timezone
from contextlib import contextmanager
from contextvars import ContextVar
from collections.abc import Callable, Iterator, Awaitable

from arclet.entari import Session, MessageChain
from arclet.letoderea import Scope
from arclet.entari.event.api import SendResponse

from .core.tool_trace import current_tool_execution_ref
from .core.agent_trace import AgentEventDraft, AgentTurnRecorder
from .agent_attachments import store_agent_attachment, remove_agent_attachments
from .core.image_source import IMAGE_FETCH_MAX_BYTES, fetch_image_bytes
from .delivery_projection import _Projection
from .web.reference_capture import _fetch_public_direct_image

_REMOTE_CAPTURE_SECONDS = 1.0
_MAX_INLINE_SOURCE_CHARS = ((IMAGE_FETCH_MAX_BYTES + 2) // 3) * 4 + 256
_CURRENT: ContextVar[DeliveryAudit | None] = ContextVar("llm_chat_delivery_audit", default=None)


def _iso(value: datetime) -> str:
    return value.replace(tzinfo=timezone.utc).isoformat()


async def _drain_tasks(tasks: set[asyncio.Task[None]]) -> None:
    cancelled = False
    while tasks:
        batch = asyncio.gather(*tuple(tasks), return_exceptions=True)
        while not batch.done():
            try:
                await asyncio.shield(batch)
            except asyncio.CancelledError:
                cancelled = True
        batch.result()
    if cancelled:
        raise asyncio.CancelledError


class DeliveryAudit:
    """One incoming generation; child tasks inherit this object, not a global turn."""

    def __init__(self, session: Session, warn: Callable[[str], object], *, attachment_root: Path | None = None):
        self.received_at = datetime.utcnow()
        self.started = time.monotonic()
        self.session = session
        self.warn = warn
        self.attachment_root = attachment_root
        self.recorder = AgentTurnRecorder(warn=self._warn)
        self.bound = False
        self.closed = False
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()

    def _warn(self, message: str) -> None:
        try:
            self.warn(message)
        except Exception:
            pass

    def matches(self, event: SendResponse) -> bool:
        return (
            not self.closed
            and bool(event.result)
            and event.session is not None
            and event.session.event is self.session.event
            and event.account.platform == self.session.account.platform
            and event.account.self_id == self.session.account.self_id
            and event.channel == self.session.channel.id
        )

    def bind(self, recorder: AgentTurnRecorder) -> None:
        """Attach the eventual turn sink, retaining sends made during preprocessing."""
        if self.bound:
            if self.recorder is not recorder:
                self._warn("delivery audit refused a different generation recorder")
            return
        recorder.append(
            "turn_timing",
            payload={"received_at": _iso(self.received_at), "timing_source": "received_to_confirmed_delivery"},
            model_visible=False,
            created_at=self.received_at,
        )
        for draft in self.recorder.events:
            self._append(recorder, draft)
        self.recorder = recorder
        self.bound = True

    @staticmethod
    def _append(recorder: AgentTurnRecorder, draft: AgentEventDraft) -> None:
        recorder.append(
            draft.event_type,
            role=draft.role,
            execution_ref=draft.execution_ref,
            payload=draft.payload,
            status=draft.status,
            effect=draft.effect,
            duration_ms=draft.duration_ms,
            model_visible=False,
            created_at=draft.created_at,
        )

    def enqueue(
        self, message: MessageChain, confirmed_at: datetime, elapsed: int, execution_ref: str
    ) -> asyncio.Task[None]:
        # Snapshot immutable text/source strings while the actual sent chain is
        # still at the receipt boundary. No storage or network I/O is done here.
        projection = _Projection(self.session)
        try:
            projection.walk(message.content)
            content = projection.content()
        except Exception as exc:
            self._warn(f"delivery projection failed: {type(exc).__name__}")
            projection = _Projection(self.session)
            projection.partial = True
            content = "[已确认发送，内容捕获失败]"
        task = asyncio.create_task(self._capture_guarded(projection, content, confirmed_at, elapsed, execution_ref))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def drain(self) -> None:
        """Settle confirmed receipts before finalizing the turn; never cancel them."""
        await _drain_tasks(self._tasks)

    async def _capture_guarded(
        self, projection: _Projection, content: str, confirmed_at: datetime, elapsed: int, execution_ref: str
    ) -> None:
        try:
            await self.capture(projection, content, confirmed_at, elapsed, execution_ref)
        except asyncio.CancelledError:
            self._warn("delivery audit persistence cancelled after confirmation")
        except Exception as exc:
            self._warn(f"delivery audit failed after confirmation: {type(exc).__name__}")

    async def capture(
        self, projection: _Projection, content: str, confirmed_at: datetime, elapsed: int, execution_ref: str
    ) -> None:
        # FIFO admission keeps simultaneous sends in their receipt order, not download order.
        async with self._lock:
            attachments: list[dict[str, object]] = []
            appended = False
            deadline = time.monotonic() + _REMOTE_CAPTURE_SECONDS
            try:
                for index, source in projection.images:
                    status = "unavailable"
                    try:
                        if source.startswith(("data:", "base64://")):
                            data = (
                                await fetch_image_bytes(self.session, source)
                                if len(source) <= _MAX_INLINE_SOURCE_CHARS
                                else None
                            )
                            provenance = "confirmed_inline"
                        elif source.startswith(("https://", "http://")) and time.monotonic() < deadline:
                            # Same public resolver, redirect checks and streaming byte limit as
                            # authorized web-image capture. Never use Session.download here.
                            result = await asyncio.wait_for(
                                _fetch_public_direct_image(source),
                                timeout=max(0.001, deadline - time.monotonic()),
                            )
                            data = result[0] if result is not None else None
                            provenance = "confirmed_public_url"
                        else:
                            data = None
                            provenance = ""
                            status = "unsupported"
                        if data is not None:
                            attachments.append(
                                store_agent_attachment(
                                    data,
                                    kind="output",
                                    source=provenance,
                                    index=index,
                                    label=f"已发送图片 {index}",
                                    root=self.attachment_root,
                                )
                            )
                            continue
                    except asyncio.CancelledError:
                        status = "cancelled"
                    except Exception as exc:
                        status = "failed"
                        self._warn(f"delivery image attachment failed: {type(exc).__name__}")
                    projection.media.append({"kind": "image", "label": f"图片 {index}", "capture_status": status})
                    projection.partial = True
                payload: dict[str, object] = {
                    "content": content,
                    "attachments": attachments,
                    "received_at": _iso(self.received_at),
                    "confirmed_at": _iso(confirmed_at),
                    "capture_status": "partial"
                    if projection.partial
                    else "redacted"
                    if projection.redacted
                    else "complete",
                }
                if projection.media:
                    payload["media"] = projection.media
                self.recorder.append(
                    "message_delivery",
                    role="assistant",
                    status="confirmed",
                    effect="confirmed",
                    model_visible=False,
                    execution_ref=execution_ref,
                    created_at=confirmed_at,
                    duration_ms=elapsed,
                    payload=payload,
                )
                appended = True
                await self.recorder.flush()
            finally:
                if not appended or (self.closed and not self.bound):
                    remove_agent_attachments(attachments, root=self.attachment_root)

    def close(self) -> None:
        self.closed = True
        if not self.bound:
            # No AgentTurn was created (e.g. preprocessing declined this input).
            for event in self.recorder.events:
                attachments = event.payload.get("attachments")
                if isinstance(attachments, list):
                    remove_agent_attachments(
                        [item for item in attachments if isinstance(item, dict)], root=self.attachment_root
                    )


@contextmanager
def delivery_audit_scope(
    session: Session, warn: Callable[[str], object], *, attachment_root: Path | None = None
) -> Iterator[DeliveryAudit]:
    audit = DeliveryAudit(session, warn, attachment_root=attachment_root)
    token = _CURRENT.set(audit)
    try:
        yield audit
    finally:
        audit.close()
        _CURRENT.reset(token)


def current_delivery_audit() -> DeliveryAudit | None:
    audit = _CURRENT.get()
    return audit if audit is not None and not audit.closed else None


def install_delivery_audit() -> Callable[[], Awaitable[None]]:
    """Register once per plugin load; give the returned disposer to plugin.collect_disposes."""
    scope = Scope.of()
    pending: set[asyncio.Task[None]] = set()

    async def observe(event: SendResponse) -> None:
        # Entari has parsed MessageObjects before this event. Sample before
        # projection, storage, network, locks or any persistence sink.
        confirmed_at = datetime.utcnow()
        confirmed_monotonic = time.monotonic()
        audit = current_delivery_audit()
        if audit is None:
            return
        try:
            if not audit.matches(event):
                return
            elapsed = max(0, round((confirmed_monotonic - audit.started) * 1000))
            task = audit.enqueue(event.message, confirmed_at, elapsed, current_tool_execution_ref())
            pending.add(task)
            task.add_done_callback(pending.discard)
        except Exception as exc:
            audit._warn(f"delivery audit failed after confirmation: {type(exc).__name__}")
        # No await: optional audit work cannot delay/fail a confirmed sender or
        # swallow its external cancellation before DeliveryState marks success.

    scope.register(observe, event=SendResponse, priority=-1000)

    async def dispose() -> None:
        disposals = scope.dispose()
        try:
            await _drain_tasks(pending)
        finally:
            if disposals:
                await asyncio.gather(*disposals, return_exceptions=True)

    return dispose


__all__ = ["DeliveryAudit", "delivery_audit_scope", "current_delivery_audit", "install_delivery_audit"]
