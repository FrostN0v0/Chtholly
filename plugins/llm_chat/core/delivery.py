"""Import-safe delivery pacing and budgeting for llm_chat generations."""

from __future__ import annotations

import re
import math
import time
from typing import Literal
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import field, dataclass
from collections.abc import Mapping, Callable, Iterator, Sequence, Awaitable

from .media import has_meaningful_text, strip_internal_media_records
from .media_delivery import strip_media_unavailable_marker

DeliveryMode = Literal["segments"]
_END_OF_RESPONSE = "[END_OF_RESPONSE]"
_MIN_INTERVAL_HARD_FLOOR = 1.1
_MAX_INTERVAL_HARD_CEILING = 5.0
_TRAILING_END_OF_RESPONSE = re.compile(rf"(?:\s*{re.escape(_END_OF_RESPONSE)})+\s*$")
_INTERNAL_PARTICIPANT_REF = re.compile(r"(?<!\w)participant_[0-9a-f]{10}(?!\w)", re.IGNORECASE)
_INTERNAL_IMAGE_REF = re.compile(
    r"(?<!\w)(?:web_ref_[0-9a-f]{24}|(?:input|reference|output)_[0-9a-f]{32})(?!\w)",
    re.IGNORECASE,
)
_PREPARED_MEDIA_REF = re.compile(r"(?<!\w)media_[0-9a-f]{32}(?!\w)", re.IGNORECASE)


@dataclass(frozen=True)
class DeliveryLimits:
    min_interval_seconds: float
    default_interval_seconds: float
    max_interval_seconds: float
    max_text_messages: int
    max_text_chars_per_message: int
    max_forward_nodes: int
    max_forward_chars_per_node: int
    max_total_text_chars: int
    max_media_messages: int


DEFAULT_DELIVERY_LIMITS = DeliveryLimits(
    min_interval_seconds=1.1,
    default_interval_seconds=1.2,
    max_interval_seconds=5.0,
    max_text_messages=5,
    max_text_chars_per_message=1000,
    max_forward_nodes=20,
    max_forward_chars_per_node=2000,
    max_total_text_chars=12000,
    max_media_messages=6,
)


class DeliveryError(RuntimeError):
    """A sanitized delivery validation or execution error."""


class DeliveryRejected(DeliveryError):
    """A request rejected before any transport side effect."""


@dataclass
class DeliveryState:
    limits: DeliveryLimits = DEFAULT_DELIVERY_LIMITS
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    clock: Callable[[], float] = time.monotonic
    mode: DeliveryMode | None = None
    text_messages: int = 0
    media_messages: int = 0
    text_chars: int = 0
    last_delivery_at: float | None = None
    delivery_attempts: int = 0
    confirmed_deliveries: int = 0
    confirmed_text_deliveries: int = 0
    confirmed_media_deliveries: int = 0
    delivered_texts: list[str] = field(default_factory=list)


_DELIVERY_STATE: ContextVar[DeliveryState | None] = ContextVar(
    "llm_chat_delivery_state",
    default=None,
)


def _finite_or_default(value: float, default: float) -> float:
    try:
        normalized = float(value)
    except (TypeError, ValueError):
        return default
    return normalized if math.isfinite(normalized) else default


def _clamp_float(value: float, minimum: float, maximum: float) -> float:
    return min(maximum, max(minimum, value))


def _tighten_int(value: int, hard_ceiling: int) -> int:
    return min(hard_ceiling, max(0, int(value)))


def normalize_delivery_limits(
    min_interval_seconds: float,
    default_interval_seconds: float,
    max_interval_seconds: float,
    max_text_messages: int,
    max_text_chars_per_message: int,
    max_forward_nodes: int,
    max_forward_chars_per_node: int,
    max_total_text_chars: int,
    max_media_messages: int,
) -> DeliveryLimits:
    """Clamp generation delivery settings to the immutable safety envelope."""

    minimum = _clamp_float(
        _finite_or_default(
            min_interval_seconds,
            DEFAULT_DELIVERY_LIMITS.min_interval_seconds,
        ),
        _MIN_INTERVAL_HARD_FLOOR,
        _MAX_INTERVAL_HARD_CEILING,
    )
    maximum = _clamp_float(
        _finite_or_default(
            max_interval_seconds,
            DEFAULT_DELIVERY_LIMITS.max_interval_seconds,
        ),
        minimum,
        _MAX_INTERVAL_HARD_CEILING,
    )
    default = _clamp_float(
        _finite_or_default(
            default_interval_seconds,
            DEFAULT_DELIVERY_LIMITS.default_interval_seconds,
        ),
        minimum,
        maximum,
    )
    total_chars = _tighten_int(
        max_total_text_chars,
        DEFAULT_DELIVERY_LIMITS.max_total_text_chars,
    )
    return DeliveryLimits(
        min_interval_seconds=minimum,
        default_interval_seconds=default,
        max_interval_seconds=maximum,
        max_text_messages=_tighten_int(
            max_text_messages,
            DEFAULT_DELIVERY_LIMITS.max_text_messages,
        ),
        max_text_chars_per_message=min(
            total_chars,
            _tighten_int(
                max_text_chars_per_message,
                DEFAULT_DELIVERY_LIMITS.max_text_chars_per_message,
            ),
        ),
        max_forward_nodes=_tighten_int(
            max_forward_nodes,
            DEFAULT_DELIVERY_LIMITS.max_forward_nodes,
        ),
        max_forward_chars_per_node=min(
            total_chars,
            _tighten_int(
                max_forward_chars_per_node,
                DEFAULT_DELIVERY_LIMITS.max_forward_chars_per_node,
            ),
        ),
        max_total_text_chars=total_chars,
        max_media_messages=_tighten_int(
            max_media_messages,
            DEFAULT_DELIVERY_LIMITS.max_media_messages,
        ),
    )


def strip_trailing_end_of_response(text: str) -> str:
    """Remove one or more reserved end markers only from the response tail."""

    return _TRAILING_END_OF_RESPONSE.sub("", text).rstrip()


def contains_internal_participant_reference(value: object) -> bool:
    """Detect opaque participant references inside model-authored tool arguments."""

    if isinstance(value, str):
        return _INTERNAL_PARTICIPANT_REF.search(value) is not None
    if isinstance(value, Mapping):
        return any(
            contains_internal_participant_reference(key) or contains_internal_participant_reference(item)
            for key, item in value.items()
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(contains_internal_participant_reference(item) for item in value)
    return False


def contains_internal_image_reference(value: object) -> bool:
    """Detect opaque image references inside model-authored values."""

    if isinstance(value, str):
        return _INTERNAL_IMAGE_REF.search(value) is not None
    if isinstance(value, Mapping):
        return any(
            contains_internal_image_reference(key) or contains_internal_image_reference(item)
            for key, item in value.items()
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(contains_internal_image_reference(item) for item in value)
    return False


def clean_delivery_fragment(text: object, *, field: str) -> str:
    """Validate one fragment without trimming spacing or requiring standalone meaning."""
    if not isinstance(text, str):
        raise DeliveryRejected(f"{field} must be a string")
    if _END_OF_RESPONSE in text or "[MEDIA_UNAVAILABLE]" in text or strip_internal_media_records(text) != text:
        raise DeliveryRejected("Message fragments cannot contain reserved control records")
    cleaned = _INTERNAL_PARTICIPANT_REF.sub("[member]", text)
    cleaned = _INTERNAL_IMAGE_REF.sub("[image]", cleaned)
    return _PREPARED_MEDIA_REF.sub("[media]", cleaned)


def normalize_delivery_text(text: object, *, field: str) -> str:
    """Return model-authored delivery text without internal control records."""

    if not isinstance(text, str):
        raise DeliveryRejected(f"{field} must be a string")
    normalized = strip_trailing_end_of_response(
        strip_media_unavailable_marker(strip_internal_media_records(text).strip())
    )
    normalized = _INTERNAL_PARTICIPANT_REF.sub("该成员", normalized)
    normalized = _INTERNAL_IMAGE_REF.sub("该图片", normalized)
    normalized = _PREPARED_MEDIA_REF.sub("[media]", normalized)
    if not has_meaningful_text(normalized):
        raise DeliveryRejected("Delivery text is empty, punctuation-only, or reserved for internal control")
    return normalized


@contextmanager
def llm_chat_delivery_scope(state: DeliveryState) -> Iterator[None]:
    """Expose one isolated delivery state during an llm_chat generation."""

    token = _DELIVERY_STATE.set(state)
    try:
        yield
    finally:
        _DELIVERY_STATE.reset(token)


def current_llm_chat_delivery() -> DeliveryState | None:
    """Return the active delivery state without requiring llm_chat scope."""

    return _DELIVERY_STATE.get()


def require_llm_chat_delivery() -> DeliveryState:
    """Reject text delivery outside the active llm_chat generation."""

    state = current_llm_chat_delivery()
    if state is None:
        raise DeliveryRejected("Delivery tools are unavailable outside llm_chat generation")
    return state


def _reserve_message_for_state(
    state: DeliveryState, text: str, *, media_count: int, text_message: bool
) -> DeliveryState:
    if not isinstance(text, str) or (not has_meaningful_text(text) and media_count == 0):
        raise DeliveryRejected("Message must contain visible text, a mention, emoji, or media")
    if type(media_count) is not int or media_count < 0:
        raise DeliveryRejected("Invalid media segment count")
    if text_message and state.text_messages >= state.limits.max_text_messages:
        raise DeliveryRejected("send_msg message budget exhausted")
    if text_message and len(text) > state.limits.max_text_chars_per_message:
        raise DeliveryRejected("send_msg exceeds the per-message character limit")
    if state.text_chars + len(text) > state.limits.max_total_text_chars:
        raise DeliveryRejected("send_msg exceeds the total character limit")
    if state.media_messages + media_count > state.limits.max_media_messages:
        raise DeliveryRejected("Media delivery budget exhausted")
    state.mode = "segments"
    state.text_messages += int(text_message)
    state.text_chars += len(text)
    state.media_messages += media_count
    return state


def reserve_message(text: str, *, media_count: int = 0, text_message: bool = True) -> DeliveryState:
    """Reserve one complete message atomically without rewriting its ordered projection."""
    return _reserve_message_for_state(
        require_llm_chat_delivery(), text, media_count=media_count, text_message=text_message
    )


def reserve_media_messages_for_state(state: DeliveryState, count: int) -> DeliveryState:
    """Atomically reserve media deliveries for a completed generation state."""

    if type(count) is not int or count < 1:
        raise DeliveryRejected("Media delivery count must be a positive integer")
    if state.media_messages + count > state.limits.max_media_messages:
        raise DeliveryRejected("Media delivery budget exhausted")
    state.media_messages += count
    return state


def reserve_media_messages(count: int) -> DeliveryState:
    """Atomically reserve media deliveries regardless of prior text delivery."""

    return reserve_media_messages_for_state(require_llm_chat_delivery(), count)


def reserve_media_message() -> DeliveryState:
    """Reserve one media delivery regardless of prior text delivery."""

    return reserve_media_messages(1)


def reserve_final_text_messages(state: DeliveryState, text: object) -> tuple[str, ...]:
    """Keep one final response as one message; only model tool calls split messages."""
    return (reserve_final_text(state, text),)


def reserve_final_text(state: DeliveryState, text: object) -> str:
    """Reserve one final supplement using the same budgets as send_msg."""
    normalized = normalize_delivery_text(text, field="text")
    _reserve_message_for_state(state, normalized, media_count=0, text_message=True)
    return normalized


def normalize_delivery_delay(delay_seconds: object) -> float | None:
    """Validate a model-provided target delay without exposing its value."""

    if delay_seconds is None:
        return None
    if isinstance(delay_seconds, bool) or not isinstance(delay_seconds, (int, float)):
        raise DeliveryRejected("delay_seconds must be a number or null")
    normalized = float(delay_seconds)
    return normalized if math.isfinite(normalized) else None


def delivery_interval_seconds(limits: DeliveryLimits, delay_seconds: float | None = None) -> float:
    target = limits.default_interval_seconds if delay_seconds is None else delay_seconds
    return _clamp_float(target, limits.min_interval_seconds, limits.max_interval_seconds)


async def wait_for_delivery(
    state: DeliveryState,
    delay_seconds: float | None = None,
) -> None:
    """Wait until the requested, safely clamped inter-message interval elapses."""

    if state.last_delivery_at is None:
        return
    target = delivery_interval_seconds(state.limits, delay_seconds)
    elapsed = max(0.0, state.clock() - state.last_delivery_at)
    remaining = target - elapsed
    if remaining > 0.0:
        await state.sleep(remaining)


def mark_delivery_attempt(state: DeliveryState) -> None:
    """Record a send attempt whose remote outcome may be unknown."""

    state.delivery_attempts += 1
    state.last_delivery_at = state.clock()


def mark_delivery_success(
    state: DeliveryState,
    texts: Sequence[str] = (),
    *,
    media: bool | int = False,
    text_message: bool | None = None,
) -> None:
    """Record a confirmed send and append delivered texts in order."""

    state.delivery_attempts += 1
    state.confirmed_deliveries += 1
    if text_message if text_message is not None else bool(texts):
        state.confirmed_text_deliveries += 1
    if media:
        state.confirmed_media_deliveries += int(media)
    state.last_delivery_at = state.clock()
    state.delivered_texts.extend(texts)


def render_delivered_text(state: DeliveryState) -> str:
    """Render confirmed text deliveries as one logical assistant turn."""

    return "\n\n".join(state.delivered_texts)
