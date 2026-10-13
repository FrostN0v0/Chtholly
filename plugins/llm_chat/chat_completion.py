"""Settle one final candidate only after its latest input revision is current."""

from __future__ import annotations

from typing import TYPE_CHECKING

from arclet.entari import Session

from .steering import SteerPending, current_steering, check_steer_boundary
from .generation import response_content
from .reaction_feedback import MessageReactionFeedback

if TYPE_CHECKING:
    from .agent_turn_setup import PreparedAgentTurn


async def settle_chat_response(
    session: Session,
    prepared: PreparedAgentTurn,
    response: object,
    reaction: MessageReactionFeedback | None,
) -> str:
    turn = prepared.lifecycle
    state = prepared.delivery_state
    resolution = prepared.resolution
    if state.delivery_attempts > state.confirmed_deliveries or any(
        event.status in {"failed", "cancelled"} and event.effect in {"partial", "unknown"}
        for event in turn.tool_trace.events
    ):
        await turn.preserve_and_rollback()
        status = "partial" if state.confirmed_deliveries else "failed"
        if reaction is not None:
            await reaction.finish(status)
        return status

    check_steer_boundary()
    outcome = resolution.outcome
    if outcome == "silent":
        status = "silent"
    elif outcome == "declined":
        previous = state.confirmed_deliveries
        if not resolution.reply or not await turn.deliver_model_reply(session, resolution.reply):
            raise RuntimeError("explicit refusal was not confirmed")
        if state.confirmed_deliveries <= previous:
            raise RuntimeError("explicit refusal was suppressed without new delivery")
        status = "declined"
    else:
        if outcome == "delivered" and not state.confirmed_deliveries:
            raise RuntimeError("explicit delivered outcome lacked confirmed output")
        if not await turn.deliver_model_reply(session, "" if outcome == "delivered" else response_content(response)):
            status = "partial" if state.confirmed_deliveries else "failed"
            if reaction is not None:
                await reaction.finish(status)
            return status
        status = "completed"

    steering = current_steering()
    if steering is not None and not steering.seal():
        raise SteerPending("New user input arrived before final settlement")
    turn.capture_tool_events()
    if status != "silent":
        await turn.persist_delivered_text()
    turn.agent_events.append(
        "response_decision",
        role="assistant",
        model_visible=False,
        payload={
            "outcome": outcome,
            "source": resolution.source if resolution.explicit else "runtime",
            "reason": resolution.reason[:240] if resolution.explicit else "",
            "actual_delivery": {
                "text_messages": state.confirmed_text_deliveries,
                "media_messages": state.confirmed_media_deliveries,
                "confirmed_deliveries": state.confirmed_deliveries,
            },
        },
    )
    if reaction is not None:
        if status == "silent":
            await reaction.finish_silent()
        else:
            await reaction.finish("declined" if status == "declined" else "success")
    return status
