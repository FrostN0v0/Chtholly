"""Explicit autonomous termination through the native LLM tool bridge."""

from __future__ import annotations

from typing import Literal
from dataclasses import replace

from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from utils.turn_resolution_core import current_turn_resolution

from ..core.types import JSONType
from ._registration import register_tool
from ..core.delivery import (
    DeliveryError,
    normalize_delivery_text,
    current_llm_chat_delivery,
    reserve_final_text_messages,
)
from ..core.tool_trace import current_tool_trace
from ..core.tool_trace_policy import _DELIVERY_TOOLS, _OBSERVATION_TOOLS

_MAX_REASON = 240
_MAX_REPLY = 600


def _text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        raise DeliveryError("finish_turn fields must be strings")
    value = value.strip()
    if len(value) > limit:
        raise DeliveryError("finish_turn field is too long")
    return value


def _check_effects(outcome: str) -> None:
    trace = current_tool_trace()
    if trace is None:
        return
    for event in trace.events:
        if event.tool_name == "finish_turn":
            continue
        if event.status == "pending" or event.effect in {"partial", "unknown"}:
            raise DeliveryError("Cannot finish while a tool effect is unresolved")
        if event.status in {"failed", "cancelled"} and event.tool_name not in _OBSERVATION_TOOLS:
            raise DeliveryError("Cannot finish after an unsettled write or delivery failure")
        if (
            outcome in {"silent", "declined"}
            and event.effect == "confirmed"
            and event.tool_name not in _DELIVERY_TOOLS
            and event.tool_name not in _OBSERVATION_TOOLS
        ):
            raise DeliveryError("Cannot silently discard or decline an already committed change")


async def finish_turn(
    outcome: Literal["silent", "declined", "delivered"],
    reason: str = "",
    reply: str = "",
) -> dict[str, JSONType]:
    """End this turn deliberately rather than returning an ambiguous empty result.

    Args:
        outcome: silent sends nothing; declined sends the supplied refusal; delivered ends after confirmed output.
        reason: A short state or factual cause for private administration, never inner reasoning.
        reply: A short truthful refusal, required only for declined; leave empty for silent or delivered.
    """
    resolution = current_turn_resolution()
    state = current_llm_chat_delivery()
    if resolution is None or state is None:
        raise DeliveryError("finish_turn requires an active generation and delivery context")
    if resolution.explicit:
        raise DeliveryError("This turn has already ended")
    if outcome not in {"silent", "declined", "delivered"}:
        raise DeliveryError("outcome must be silent, declined, or delivered")
    _check_effects(outcome)
    reason = _text(reason, _MAX_REASON)
    reply = _text(reply, _MAX_REPLY)
    if state.delivery_attempts > state.confirmed_deliveries:
        raise DeliveryError("Cannot finish while a send result is unconfirmed")
    if outcome == "silent":
        if state.delivery_attempts or state.confirmed_deliveries:
            raise DeliveryError("Cannot silently finish after delivery has started")
        if reply:
            raise DeliveryError("Silent finish cannot include reply text")
    elif outcome == "declined":
        reply = normalize_delivery_text(reply, field="reply")
        if state.confirmed_media_deliveries:
            raise DeliveryError("Cannot decline after media delivery")
        reserve_final_text_messages(replace(state), reply)
    else:
        if state.confirmed_deliveries <= 0:
            raise DeliveryError("Delivered finish requires confirmed output")
        if reply:
            raise DeliveryError("Delivered finish does not accept replacement text")
    resolution.set(outcome, reason=reason, reply=reply, source="model")
    return {"outcome": outcome}


def register_finish_turn(dispatcher: PluginDispatcher[JSONType]) -> Subscriber[JSONType]:
    return register_tool(dispatcher, finish_turn)
