"""Generation-local references for selectively accessed channel images."""

from __future__ import annotations

import asyncio
import secrets
from contextlib import contextmanager
from contextvars import Token, ContextVar
from dataclasses import field, dataclass
from collections.abc import Iterator

from arclet.entari import Session

from .config import LLMChatConfig
from .perception import ChannelPerceptionLike

MAX_CHANNEL_IMAGE_REFERENCES = 32
MAX_CHANNEL_MESSAGE_IMAGES = 32


@dataclass(frozen=True, slots=True)
class MessageImageTarget:
    cursor: str
    image_index: int


@dataclass(frozen=True, slots=True)
class ParticipantAvatarTarget:
    participant_ref: str
    source: str


ChannelImageTarget = MessageImageTarget | ParticipantAvatarTarget


class ChannelImageReferenceError(RuntimeError):
    """A generation-local channel image reference cannot be resolved."""


@dataclass(slots=True)
class ChannelImageReferences:
    """Authorize opaque image references for one model generation only."""

    _targets: dict[str, ChannelImageTarget] = field(default_factory=dict)
    _reverse: dict[ChannelImageTarget, str] = field(default_factory=dict)

    def _register(self, target: ChannelImageTarget) -> str | None:
        if existing := self._reverse.get(target):
            return existing
        if len(self._targets) >= MAX_CHANNEL_IMAGE_REFERENCES:
            return None
        while True:
            reference = f"channel_image_{secrets.token_urlsafe(9)}"
            if reference not in self._targets:
                break
        self._targets[reference] = target
        self._reverse[target] = reference
        return reference

    def register_message(self, cursor: str, image_index: int) -> str | None:
        return self._register(MessageImageTarget(cursor, image_index))

    def register_avatar(self, participant_ref: str, source: str) -> str | None:
        return self._register(ParticipantAvatarTarget(participant_ref, source))

    def resolve(self, reference: str) -> ChannelImageTarget | None:
        return self._targets.get(reference)

    @property
    def remaining_capacity(self) -> int:
        return max(0, MAX_CHANNEL_IMAGE_REFERENCES - len(self._targets))


_ACTIVE_CHANNEL_IMAGE_REFERENCES: ContextVar[ChannelImageReferences | None] = ContextVar(
    "llm_chat_channel_image_references",
    default=None,
)


@contextmanager
def llm_chat_channel_image_scope(references: ChannelImageReferences) -> Iterator[None]:
    token: Token[ChannelImageReferences | None] = _ACTIVE_CHANNEL_IMAGE_REFERENCES.set(references)
    try:
        yield
    finally:
        _ACTIVE_CHANNEL_IMAGE_REFERENCES.reset(token)


def current_channel_image_references() -> ChannelImageReferences | None:
    return _ACTIVE_CHANNEL_IMAGE_REFERENCES.get()


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return 0
    try:
        return max(0, int(value))
    except ValueError:
        return 0


def attach_channel_image_references(
    config: LLMChatConfig,
    messages: list[dict[str, object]],
    references: ChannelImageReferences,
) -> None:
    """Attach bounded opaque references without fetching or recognizing images."""

    remaining = min(
        MAX_CHANNEL_MESSAGE_IMAGES,
        references.remaining_capacity,
        max(0, int(config.channel_message_max_images)),
    )
    for message in reversed(messages):
        image_count = _nonnegative_int(message.get("image_count"))
        cursor = str(message.get("cursor", "")).strip()
        if image_count == 0 or not cursor:
            continue
        if remaining == 0:
            message["images_unavailable"] = image_count
            continue

        selected_count = min(image_count, remaining)
        image_views: list[dict[str, object]] = []
        for image_index in range(1, selected_count + 1):
            reference = references.register_message(cursor, image_index)
            if reference is None:
                break
            image_views.append({"image_ref": reference})
        if image_views:
            message["images"] = image_views
        unavailable = image_count - len(image_views)
        if unavailable:
            message["images_unavailable"] = unavailable
        remaining -= len(image_views)


async def resolve_channel_image_source(
    session: Session,
    image_ref: str,
    perception: ChannelPerceptionLike,
    *,
    allow_avatar: bool = True,
) -> str:
    """Resolve one authorized channel image source for a current-generation tool."""

    references = current_channel_image_references()
    target = references.resolve(image_ref.strip()) if references is not None else None
    if allow_avatar and isinstance(target, ParticipantAvatarTarget):
        return target.source
    if not isinstance(target, MessageImageTarget):
        raise ChannelImageReferenceError("A valid image_ref from current channel history is required")
    try:
        sources = await perception.message_image_sources(session, target.cursor)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise ChannelImageReferenceError("The channel image is no longer available") from exc
    if target.image_index > len(sources):
        raise ChannelImageReferenceError("The channel image is no longer available")
    return sources[target.image_index - 1]
