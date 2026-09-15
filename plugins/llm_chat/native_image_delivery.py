"""Current-response native images across Agno pause/continue and confirmed delivery."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import field, dataclass
from collections.abc import Callable, Iterator, Awaitable

from agno.media import Image

from .core.delivery import DEFAULT_DELIVERY_LIMITS, DeliveryError, current_llm_chat_delivery
from .image_edit_refs import current_image_edit_references

NativeImageSender = Callable[[object], Awaitable[bool]]


class NativeImageDeliveryError(DeliveryError):
    """A native delivery failed and the generation must not replay its effects."""


@dataclass(frozen=True, slots=True)
class _ImageBatch:
    images: tuple[Image, ...]


@dataclass
class NativeImageBuffer:
    sender: NativeImageSender | None
    owner: object | None = None
    images: list[Image] = field(default_factory=list)
    failed: bool = False
    closed: bool = False

    def capture(self, owner: object, images: tuple[Image, ...]) -> bool:
        if self.closed:
            raise NativeImageDeliveryError("Native image response outlived its generation")
        if self.owner is None:
            self.owner = owner
        if self.owner is not owner:
            return False
        references = current_image_edit_references()
        if references is not None and (references.requires_image_edit or references.edit_confirmed):
            return True
        state = current_llm_chat_delivery()
        remaining = (
            state.limits.max_media_messages - state.media_messages
            if state is not None
            else DEFAULT_DELIVERY_LIMITS.max_media_messages
        )
        if len(self.images) + len(images) > remaining:
            raise NativeImageDeliveryError("Native images exceed the remaining media delivery budget")
        self.images.extend(images)
        return True

    def check(self) -> None:
        if self.failed:
            raise NativeImageDeliveryError("Native image delivery failed; do not replay the confirmed prefix")

    async def deliver(self) -> None:
        self.check()
        if not self.images:
            return
        if self.closed or self.sender is None:
            self.failed = True
            raise NativeImageDeliveryError("Native images require an active confirmed-delivery boundary")
        batch = _ImageBatch(tuple(self.images))
        # Consume before awaiting: cancellation and unknown receipts must never replay this batch.
        self.images.clear()
        try:
            if not await self.sender(batch):
                raise NativeImageDeliveryError("Native image delivery was not confirmed")
        except BaseException:
            self.failed = True
            raise

    async def complete(self, response: object) -> None:
        self.check()
        if self.owner is None:
            return
        if self.sender is not None:
            await self.deliver()
        # This is the existing extraction cache, not an alternative history or image store.
        setattr(response, "_llm_chat_native_images", tuple(self.images))
        self.images.clear()


_SENDER: ContextVar[NativeImageSender | None] = ContextVar("llm_chat_native_image_sender", default=None)
_BUFFER: ContextVar[NativeImageBuffer | None] = ContextVar("llm_chat_native_image_buffer", default=None)


@contextmanager
def native_image_delivery_scope(sender: NativeImageSender) -> Iterator[None]:
    token = _SENDER.set(sender)
    try:
        yield
    finally:
        _SENDER.reset(token)


@contextmanager
def capture_native_images_scope() -> Iterator[NativeImageBuffer]:
    buffer = NativeImageBuffer(_SENDER.get())
    token = _BUFFER.set(buffer)
    try:
        yield buffer
    finally:
        buffer.closed = True
        buffer.images.clear()
        _BUFFER.reset(token)


def capture_native_images(owner: object, images: tuple[Image, ...]) -> bool:
    buffer = _BUFFER.get()
    return buffer.capture(owner, images) if buffer is not None else False


async def send_pending_native_images() -> None:
    buffer = _BUFFER.get()
    if buffer is not None:
        await buffer.deliver()


def discard_pending_native_images() -> None:
    buffer = _BUFFER.get()
    if buffer is not None:
        buffer.images.clear()


def check_native_image_delivery() -> None:
    buffer = _BUFFER.get()
    if buffer is not None:
        buffer.check()
