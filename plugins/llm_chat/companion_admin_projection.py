"""Bounded read-only views of native companion audits, never current-policy guesses."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from collections.abc import Mapping, Sequence

from .models import AgentEvent
from .agent_events import load_event_payload

REVIEW_STATUSES = frozenset({"active", "completed", "failed", "cancelled", "expired"})
_PRIVATE_KEYS = frozenset(
    {
        "scope_id",
        "account_id",
        "channel_id",
        "user_id",
        "platform_user_id",
        "unified_user_id",
        "self_id",
        "lease_token",
        "lease_deadline",
        "review_token",
        "review_deadline",
        "receipts",
        "confirmed_receipts_json",
        "identity",
        "reasoning_content",
        "thinking",
        "chain_of_thought",
    }
)


class CompanionAdminError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid_request", status: int = 400):
        super().__init__(message)
        self.code, self.status = code, status


def utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (
        value.replace(tzinfo=timezone.utc).isoformat()
        if value.tzinfo is None
        else value.astimezone(timezone.utc).isoformat()
    )


def json_object(raw: str | None) -> dict[str, object] | None:
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def bounded_value(value: object, *, maximum: int = 32000) -> tuple[object, bool]:
    """Keep native structured data usable; overflow is explicit and originals have event links."""
    remaining, nodes, truncated = maximum, 3000, False

    def visit(item: object, depth: int = 0) -> object:
        nonlocal remaining, nodes, truncated
        nodes -= 1
        if nodes < 0 or depth > 10 or remaining <= 0:
            truncated = True
            return "[display truncated]"
        if isinstance(item, str):
            size = min(2000, remaining)
            result = item[:size]
            if len(item) > size:
                result += " [display truncated]"
                truncated = True
            remaining -= len(result)
            return result
        if isinstance(item, Mapping):
            result = {}
            for index, (key, child) in enumerate(item.items()):
                if index >= 100:
                    truncated = True
                    break
                if str(key) in _PRIVATE_KEYS:
                    continue
                result[str(key)[:100]] = visit(child, depth + 1)
            return result
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            if len(item) > 100:
                truncated = True
            return [visit(child, depth + 1) for child in item[:100]]
        return item if item is None or isinstance(item, (bool, int, float)) else None

    return visit(value), truncated


def reflection_records(events: Sequence[AgentEvent], *, review_ref: str | None = None) -> dict[str, object]:
    """Only the private native input/proposal/decision events establish historical facts."""
    state_event = proposal_event = decision_event = context_event = None
    capture = {"state": "not_recorded", "proposal": "not_recorded", "decision": "not_recorded"}
    state = proposal = decision = None
    if review_ref is None:
        for event in events:
            if event.event_type == "companion_reflection" and event.status == "started":
                value = load_event_payload(event).get("lease_token")
                if isinstance(value, str):
                    review_ref = value
                    break
    for event in events:
        payload = load_event_payload(event)
        if event.event_type == "companion_state" and not event.model_visible:
            state_event = event
            unavailable = payload.get("capture_status") in {"overflow", "invalid"} and "life" not in payload
            state = None if unavailable else payload
            capture["state"] = str(payload["capture_status"]) if unavailable else "recorded"
        elif event.event_type == "companion_reflection" and event.status == "succeeded" and not event.model_visible:
            proposal_event = event
            candidate = payload.get("proposal")
            if isinstance(candidate, dict):
                proposal = candidate
                capture["proposal"] = "recorded"
            elif payload.get("capture_status") in {"overflow", "invalid"}:
                capture["proposal"] = str(payload["capture_status"])
        elif event.event_type == "companion_contact_decision" and not event.model_visible:
            if review_ref is not None and payload.get("review_ref") == review_ref:
                decision_event = event
                decision = {**payload, "source": "companion_contact_decision", "event_ref": event.event_ref}
                capture["decision"] = "recorded"
        elif event.event_type == "context_snapshot":
            context_event = event
    return {
        "state": state,
        "proposal": proposal,
        "decision": decision,
        "events": {
            "state_event_ref": state_event.event_ref if state_event else None,
            "proposal_event_ref": proposal_event.event_ref if proposal_event else None,
            "decision_event_ref": decision_event.event_ref if decision_event else None,
            "context_event_ref": context_event.event_ref if context_event else None,
        },
        "capture": capture,
    }


def project_companion(events: Sequence[AgentEvent]) -> dict[str, object]:
    """Add host mode/trigger/reflection next to (not instead of) response_decision."""
    mode = trigger = None
    for event in events:
        if event.event_type not in {"conversation_mode", "companion_trigger"}:
            continue
        payload, truncated = bounded_value(load_event_payload(event))
        item = {**payload, "event_ref": event.event_ref, "truncated": truncated} if isinstance(payload, dict) else None
        if event.event_type == "conversation_mode":
            mode = item
        else:
            trigger = item
    reflection = None
    if any(event.event_type == "companion_reflection" for event in events):
        records = reflection_records(events)
        reflection = {"events": records["events"], "capture": records["capture"], "historical": True, "truncated": {}}
        for key in ("state", "proposal", "decision"):
            reflection[key], reflection["truncated"][key] = bounded_value(records[key])
    return {"mode": mode, "trigger": trigger, "reflection": reflection}


def contact_view(
    proposal: object, state: object, *, contacts: Sequence[Mapping[str, object]] = ()
) -> dict[str, object] | None:
    if not isinstance(proposal, Mapping):
        return None
    member_ref = proposal.get("member_ref")
    raw_target = proposal.get("target", "member")
    target = raw_target if isinstance(raw_target, str) and raw_target in {"member", "channel"} else None
    display_name = None
    if target == "member" and isinstance(state, Mapping) and isinstance(state.get("members"), list):
        display_name = next(
            (
                item.get("display_name")
                for item in state["members"]
                if isinstance(item, Mapping) and item.get("member_ref") == member_ref
            ),
            None,
        )
    if target == "member" and display_name is None:
        display_name = next(
            (item.get("display_name") for item in contacts if item.get("member_ref") == member_ref), None
        )
    return {
        "target": target,
        **{
            key: value[:400] if isinstance(value := proposal.get(key), str) else None
            for key in ("purpose", "topic", "reason", "member_ref")
        },
        "display_name": display_name[:200] if isinstance(display_name, str) else None,
    }
