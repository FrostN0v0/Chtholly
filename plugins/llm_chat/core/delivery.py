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
from collections.abc import Callable, Iterator, Sequence, Awaitable

from .media import strip_internal_media_records
from .media_delivery import strip_media_unavailable_marker

DeliveryMode = Literal["segments", "forward"]
_END_OF_RESPONSE = "[END_OF_RESPONSE]"
_MIN_INTERVAL_HARD_FLOOR = 1.1
_MAX_INTERVAL_HARD_CEILING = 5.0
_STRUCTURED_FINAL_LINE = re.compile(r"^(?:#{1,6}\s|[-*+]\s|\d+[.)]\s|>\s|\|)")
_TRAILING_END_OF_RESPONSE = re.compile(rf"(?:\s*{re.escape(_END_OF_RESPONSE)})+\s*$")


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
    max_media_messages=2,
)


class DeliveryError(RuntimeError):
    """A sanitized delivery validation or execution error."""


@dataclass
class DeliveryState:
    limits: DeliveryLimits = DEFAULT_DELIVERY_LIMITS
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    clock: Callable[[], float] = time.monotonic
    mode: DeliveryMode | None = None
    text_messages: int = 0
    forward_calls: int = 0
    media_messages: int = 0
    text_chars: int = 0
    last_delivery_at: float | None = None
    delivery_attempts: int = 0
    confirmed_deliveries: int = 0
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


def normalize_delivery_text(text: object, *, field: str) -> str:
    """Return model-authored delivery text without internal control records."""

    if not isinstance(text, str):
        raise DeliveryError(f"{field} must be a string")
    normalized = strip_trailing_end_of_response(
        strip_media_unavailable_marker(strip_internal_media_records(text).strip())
    )
    if not normalized:
        raise DeliveryError("Delivery text is empty or reserved for internal control")
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
        raise DeliveryError("Delivery tools are unavailable outside llm_chat generation")
    return state


def reserve_text_message(text: object) -> tuple[DeliveryState, str]:
    """Atomically reserve one paced text message."""

    state = require_llm_chat_delivery()
    normalized = normalize_delivery_text(text, field="text")
    if state.mode == "forward":
        raise DeliveryError("Do not mix send_text and send_merged_forward in one generation")
    if state.text_messages >= state.limits.max_text_messages:
        raise DeliveryError("send_text budget exhausted; finish with one final reply")
    if len(normalized) > state.limits.max_text_chars_per_message:
        raise DeliveryError(
            f"send_text exceeds the configured per-message character limit ({state.limits.max_text_chars_per_message})"
        )
    if state.text_chars + len(normalized) > state.limits.max_total_text_chars:
        raise DeliveryError(
            f"send_text exceeds the configured total character limit ({state.limits.max_total_text_chars})"
        )

    state.mode = "segments"
    state.text_messages += 1
    state.text_chars += len(normalized)
    return state, normalized


def reserve_forward_messages(messages: object) -> tuple[DeliveryState, tuple[str, ...]]:
    """Atomically reserve one merged-forward delivery."""

    state = require_llm_chat_delivery()
    if isinstance(messages, (str, bytes, bytearray)) or not isinstance(messages, Sequence):
        raise DeliveryError("messages must be a list of strings")
    if any(not isinstance(message, str) for message in messages):
        raise DeliveryError("messages must be a list of strings")
    if not messages:
        raise DeliveryError("messages must contain at least one text node")

    normalized = tuple(
        normalize_delivery_text(message, field=f"messages[{index}]") for index, message in enumerate(messages)
    )
    if state.mode == "segments":
        raise DeliveryError("Do not mix send_text and send_merged_forward in one generation")
    if state.forward_calls >= 1:
        raise DeliveryError("send_merged_forward budget exhausted")
    if len(normalized) > state.limits.max_forward_nodes:
        raise DeliveryError(f"send_merged_forward exceeds the configured node limit ({state.limits.max_forward_nodes})")
    oversized_index = next(
        (index for index, message in enumerate(normalized) if len(message) > state.limits.max_forward_chars_per_node),
        None,
    )
    if oversized_index is not None:
        raise DeliveryError(
            f"messages[{oversized_index}] exceeds the configured node character limit "
            f"({state.limits.max_forward_chars_per_node})"
        )
    total_chars = sum(map(len, normalized))
    if state.text_chars + total_chars > state.limits.max_total_text_chars:
        raise DeliveryError(
            f"send_merged_forward exceeds the configured total character limit ({state.limits.max_total_text_chars})"
        )

    state.mode = "forward"
    state.forward_calls += 1
    state.text_chars += total_chars
    return state, normalized


def reserve_media_messages(count: int) -> DeliveryState:
    """Atomically reserve media deliveries before any text mode begins."""

    if type(count) is not int or count < 1:
        raise DeliveryError("Media delivery count must be a positive integer")
    state = require_llm_chat_delivery()
    if state.mode is not None:
        raise DeliveryError("Media must be sent before text delivery")
    if state.media_messages + count > state.limits.max_media_messages:
        raise DeliveryError("Media delivery budget exhausted")
    state.media_messages += count
    return state


def reserve_media_message() -> DeliveryState:
    """Reserve one media delivery before any text mode begins."""

    return reserve_media_messages(1)


def _split_final_text(normalized: str) -> tuple[str, ...]:
    if "```" in normalized or "~~~" in normalized:
        return (normalized,)
    segments = tuple(part.strip() for part in re.split(r"[\r\n]+", normalized) if part.strip())
    if len(segments) < 2 or any(_STRUCTURED_FINAL_LINE.match(segment) for segment in segments):
        return (normalized,)
    return segments


def reserve_final_text_messages(state: DeliveryState, text: object) -> tuple[str, ...]:
    """Reserve natural newline-separated final text as paced chat messages when budgets allow."""

    normalized = normalize_delivery_text(text, field="text")
    segments = _split_final_text(normalized)
    if len(segments) < 2 or state.mode == "forward":
        return (reserve_final_text(state, normalized),)

    remaining_messages = state.limits.max_text_messages - state.text_messages
    total_chars = sum(map(len, segments))
    if (
        len(segments) > remaining_messages
        or any(len(segment) > state.limits.max_text_chars_per_message for segment in segments)
        or state.text_chars + total_chars > state.limits.max_total_text_chars
    ):
        return (reserve_final_text(state, normalized),)

    state.mode = "segments"
    state.text_messages += len(segments)
    state.text_chars += total_chars
    return segments


def reserve_final_text(state: DeliveryState, text: object) -> str:
    """Reserve a final supplement after model-driven text delivery."""

    normalized = normalize_delivery_text(text, field="text")
    if state.mode is None:
        return normalized
    if (
        len(normalized) > state.limits.max_text_chars_per_message
        or state.text_chars + len(normalized) > state.limits.max_total_text_chars
    ):
        raise DeliveryError("Final supplement exceeds the configured delivery text budget")
    state.text_chars += len(normalized)
    return normalized


def normalize_delivery_delay(delay_seconds: object) -> float | None:
    """Validate a model-provided target delay without exposing its value."""

    if delay_seconds is None:
        return None
    if isinstance(delay_seconds, bool) or not isinstance(delay_seconds, (int, float)):
        raise DeliveryError("delay_seconds must be a number or null")
    normalized = float(delay_seconds)
    return normalized if math.isfinite(normalized) else None


async def wait_for_delivery(
    state: DeliveryState,
    delay_seconds: float | None = None,
) -> None:
    """Wait until the requested, safely clamped inter-message interval elapses."""

    if state.last_delivery_at is None:
        return
    target = state.limits.default_interval_seconds if delay_seconds is None else delay_seconds
    target = _clamp_float(
        target,
        state.limits.min_interval_seconds,
        state.limits.max_interval_seconds,
    )
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
    media: bool = False,
) -> None:
    """Record a confirmed send and append delivered texts in order."""

    state.delivery_attempts += 1
    state.confirmed_deliveries += 1
    if media:
        state.confirmed_media_deliveries += 1
    state.last_delivery_at = state.clock()
    state.delivered_texts.extend(texts)


def render_delivered_text(state: DeliveryState) -> str:
    """Render confirmed text deliveries as one logical assistant turn."""

    return "\n\n".join(state.delivered_texts)
