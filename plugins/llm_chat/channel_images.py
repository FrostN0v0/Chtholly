"""Generation-local references and vision enrichment for recent channel images."""

from __future__ import annotations

import asyncio
import secrets
from contextlib import contextmanager
from contextvars import Token, ContextVar
from dataclasses import field, dataclass
from collections.abc import Callable, Iterator

from arclet.entari import Session

from .config import LLMChatConfig
from .vision import describe_image
from .perception import ChannelPerceptionLike

WarningSink = Callable[[str], object]
MAX_CHANNEL_IMAGE_REFERENCES = 32
MAX_DESCRIBED_CHANNEL_IMAGES = 32
_IMAGE_PLACEHOLDER = "[Image]"
_IMAGE_UNAVAILABLE = "[Image description unavailable]"


@dataclass(frozen=True, slots=True)
class MessageImageTarget:
    cursor: str
    image_index: int


@dataclass(frozen=True, slots=True)
class ParticipantAvatarTarget:
    participant_ref: str
    source: str


ChannelImageTarget = MessageImageTarget | ParticipantAvatarTarget


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


async def enrich_channel_message_images(
    config: LLMChatConfig,
    session: Session,
    perception: ChannelPerceptionLike,
    messages: list[dict[str, object]],
    references: ChannelImageReferences,
    warn: WarningSink,
) -> None:
    """Hydrate bounded image descriptions and opaque resend references in place."""

    remaining = min(
        MAX_DESCRIBED_CHANNEL_IMAGES,
        references.remaining_capacity,
        max(0, int(config.channel_message_max_described_images)),
    )
    description_jobs: list[tuple[dict[str, object], str]] = []
    for message in reversed(messages):
        image_count = _nonnegative_int(message.get("image_count"))
        cursor = str(message.get("cursor", "")).strip()
        if image_count == 0 or not cursor:
            continue
        if remaining == 0:
            message["images_unavailable"] = image_count
            continue
        try:
            sources = await perception.message_image_sources(session, cursor)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            warn(f"channel image source resolution failed: {type(exc).__name__}")
            message["images_unavailable"] = image_count
            continue

        selected_count = min(image_count, len(sources), remaining)
        image_views: list[dict[str, object]] = []
        for image_index, source in enumerate(sources[:selected_count], start=1):
            reference = references.register_message(cursor, image_index)
            if reference is None:
                break
            image_view: dict[str, object] = {
                "image_ref": reference,
                "description": _IMAGE_PLACEHOLDER,
            }
            image_views.append(image_view)
            description_jobs.append((image_view, source))
        if image_views:
            message["images"] = image_views
        unavailable = image_count - len(image_views)
        if unavailable:
            message["images_unavailable"] = unavailable
        remaining -= len(image_views)

    if not config.image_understanding_enabled or not description_jobs:
        return

    async def describe(image_view: dict[str, object], source: str) -> None:
        try:
            description = await describe_image(config, session, source)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            warn(f"channel image description failed: {type(exc).__name__}")
            description = ""
        image_view["description"] = description or _IMAGE_UNAVAILABLE

    await asyncio.gather(*(describe(image_view, source) for image_view, source in description_jobs))
