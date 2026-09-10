"""Read-only projections of captured model calls, context, and session usage."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from collections.abc import Mapping, Sequence

from sqlalchemy import JSON, case, func, select, type_coerce
from entari_plugin_database import get_session

from .models import AgentTurn, AgentEvent
from .agent_events import load_event_payload
from .agent_event_view import event_images, event_preview, event_evidence
from .core.model_audit import normalize_usage

_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_input_tokens",
    "reasoning_tokens",
)
_BUDGET_FIELDS = (
    "max_input_tokens",
    "output_reserve_tokens",
    "rollover_ratio",
    "minimum_recent_turns",
    "inline_event_chars",
)


def _preview(value: object, limit: int = 400) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _model_groups(events: Sequence[AgentEvent]) -> list[dict[str, AgentEvent]]:
    groups: dict[tuple[int, str], dict[str, AgentEvent]] = {}
    for event in events:
        if event.event_type not in {"model_request", "model_response"}:
            continue
        payload = load_event_payload(event)
        request_id = payload.get("request_id")
        key = (event.turn_id, request_id if isinstance(request_id, str) and request_id else event.event_ref)
        groups.setdefault(key, {})[event.event_type] = event
    return list(groups.values())


def summarize_turn_events(events: Sequence[AgentEvent]) -> dict[str, object]:
    """Summarize observed calls without interpreting legacy gaps as zero calls."""
    groups = _model_groups(events)
    captured = bool(groups) or any(event.event_type == "context_snapshot" for event in events)
    models: list[str] = []
    for group in groups:
        event = group.get("model_request")
        if event is None:
            continue
        model = load_event_payload(event).get("model")
        if isinstance(model, str) and model and model not in models:
            models.append(model)
    inputs = [event for event in events if event.event_type == "user_input"]
    tools = [event for event in events if event.event_type in {"assistant_tool_call", "tool_result"}]
    execution_refs = {event.execution_ref for event in tools if event.execution_ref}
    tool_count = len(execution_refs) if (tools or captured) and all(event.execution_ref for event in tools) else None
    return {
        "input_preview": event_preview(inputs[0], load_event_payload(inputs[0])) if inputs else None,
        "model": ", ".join(models) if models else None,
        "model_call_count": len(groups) if captured else None,
        "tool_call_count": tool_count,
    }


def _timestamp(value: object) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def project_turn_timing(turn: AgentTurn, events: Sequence[AgentEvent]) -> dict[str, object]:
    """Keep receipt-to-delivery measurements separate from lifecycle estimates."""
    timing = next((event for event in events if event.event_type == "turn_timing"), None)
    deliveries = [
        event
        for event in events
        if event.event_type == "message_delivery" and event.status == event.effect == "confirmed"
    ]
    last = max(deliveries, key=lambda event: event.sequence or 0) if deliveries else None
    timing_payload = load_event_payload(timing) if timing is not None else {}
    last_payload = load_event_payload(last) if last is not None else {}
    received = _timestamp(timing_payload.get("received_at")) or _timestamp(last_payload.get("received_at"))
    confirmed = (
        (_timestamp(last_payload.get("confirmed_at")) or _timestamp(last.created_at)) if last is not None else None
    )
    measured = last.duration_ms if last is not None else None
    measured = measured if isinstance(measured, int) and not isinstance(measured, bool) and measured >= 0 else None
    audited = timing is not None or any(event.event_type == "message_delivery" for event in events)
    source = "received_to_confirmed_delivery" if measured is not None else "not_recorded"
    elapsed = None
    if turn.status == "running":
        source = "running"
        started = received or _timestamp(turn.created_at)
        if started is not None:
            elapsed = max(0, int((datetime.now(timezone.utc) - started).total_seconds() * 1000))
    elif audited and last is None:
        source = "not_delivered"
    elif not audited:
        started, finished = _timestamp(turn.created_at), _timestamp(turn.finished_at)
        if started is not None and finished is not None and finished >= started:
            measured = int((finished - started).total_seconds() * 1000)
            source = "legacy_lifecycle"
    return {
        "duration_ms": measured,
        "duration_source": source,
        "received_at": received.isoformat() if received is not None else None,
        "last_delivery_at": confirmed.isoformat() if confirmed is not None else None,
        "elapsed_ms": elapsed,
    }


async def turn_list_summaries(turns: Sequence[AgentTurn]) -> dict[int, dict[str, object]]:
    """Fetch only lightweight call metadata and bounded input snippets for a page."""
    turn_ids = [turn.id for turn in turns]
    if not turn_ids:
        return {}
    payload = type_coerce(AgentEvent.payload_json, JSON)
    async with get_session() as db:
        rows = (
            await db.execute(
                select(
                    AgentEvent.turn_id,
                    AgentEvent.event_ref,
                    AgentEvent.event_type,
                    AgentEvent.execution_ref,
                    AgentEvent.sequence,
                    AgentEvent.status,
                    AgentEvent.effect,
                    AgentEvent.duration_ms,
                    AgentEvent.created_at,
                    case(
                        (
                            AgentEvent.event_type.in_(("turn_timing", "message_delivery")),
                            payload["received_at"].as_string(),
                        )
                    ),
                    case((AgentEvent.event_type == "message_delivery", payload["confirmed_at"].as_string())),
                    payload["request_id"].as_string(),
                    payload["model"].as_string(),
                    case((AgentEvent.event_type == "user_input", func.substr(payload["content"].as_string(), 1, 401))),
                )
                .where(
                    AgentEvent.turn_id.in_(turn_ids),
                    AgentEvent.event_type.in_(
                        (
                            "user_input",
                            "model_request",
                            "model_response",
                            "assistant_tool_call",
                            "tool_result",
                            "context_snapshot",
                            "turn_timing",
                            "message_delivery",
                        )
                    ),
                )
                .order_by(AgentEvent.turn_id.asc(), AgentEvent.sequence.asc())
            )
        ).all()
    grouped: dict[int, list[AgentEvent]] = {turn_id: [] for turn_id in turn_ids}
    for (
        turn_id,
        event_ref,
        event_type,
        execution_ref,
        sequence,
        status,
        effect,
        duration_ms,
        created_at,
        received_at,
        confirmed_at,
        request_id,
        model,
        content,
    ) in rows:
        grouped[turn_id].append(
            AgentEvent(
                turn_id=turn_id,
                event_ref=event_ref,
                event_type=event_type,
                execution_ref=execution_ref,
                sequence=sequence,
                status=status,
                effect=effect,
                duration_ms=duration_ms,
                created_at=created_at,
                payload_json=json.dumps(
                    {
                        "request_id": request_id,
                        "model": model,
                        "content": content,
                        "received_at": received_at,
                        "confirmed_at": confirmed_at,
                    },
                    ensure_ascii=False,
                ),
            )
        )
    return {
        turn.id: {**summarize_turn_events(grouped[turn.id]), **project_turn_timing(turn, grouped[turn.id])}
        for turn in turns
    }


def summarize_usage(events: Sequence[AgentEvent]) -> dict[str, object]:
    """Sum recorded usage once; never substitute token estimates for provider metrics."""
    groups = _model_groups(events)
    actual_attempts = {(event.turn_id, event.attempt) for group in groups for event in group.values()}
    samples = []
    measured_requests = 0
    unknown_requests = 0
    for group in groups:
        response = group.get("model_response")
        usage = normalize_usage(load_event_payload(response).get("usage") if response is not None else None)
        if any(usage.get(key) is not None for key in _TOKEN_FIELDS):
            measured_requests += 1
        else:
            unknown_requests += 1
        samples.append(usage)
    legacy: dict[tuple[int, int], AgentEvent] = {}
    for event in events:
        key = (event.turn_id, event.attempt)
        if event.event_type == "model_attempt" and key not in actual_attempts:
            legacy[key] = event
    measured_aggregates = 0
    for event in legacy.values():
        usage = normalize_usage(load_event_payload(event).get("metrics"))
        measured_aggregates += int(any(usage.get(key) is not None for key in _TOKEN_FIELDS))
        samples.append(usage)
    totals: dict[str, object] = {}
    field_coverage = {}
    for key in _TOKEN_FIELDS:
        values = [value for sample in samples if (value := sample.get(key)) is not None]
        totals[key] = sum(values) if values else None
        field_coverage[key] = {"measured": len(values), "unknown": len(samples) - len(values)}
    source = (
        "mixed"
        if groups and legacy
        else "model_response"
        if groups
        else "legacy_aggregate"
        if legacy
        else "not_recorded"
    )
    return {
        **totals,
        "measured_requests": measured_requests,
        "unknown_requests": unknown_requests,
        "source": source,
        "coverage": {
            "fields": field_coverage,
            "measured_aggregates": measured_aggregates,
            "unknown_aggregates": len(legacy) - measured_aggregates,
            "legacy_request_count_unknown": bool(legacy),
            "complete": bool(samples)
            and not legacy
            and all(sample.get("total_tokens") is not None for sample in samples),
        },
    }


def project_context(events: Sequence[AgentEvent]) -> dict[str, object]:
    snapshot = next((event for event in reversed(events) if event.event_type == "context_snapshot"), None)
    selection_event = next((event for event in reversed(events) if event.event_type == "context_selection"), None)
    selection = load_event_payload(selection_event) if selection_event is not None else {}
    payload = load_event_payload(snapshot) if snapshot is not None else {}
    captured_selection = payload.get("selection")
    if isinstance(captured_selection, Mapping):
        selection = dict(captured_selection)
    budgets = payload.get("budgets")
    if not isinstance(budgets, Mapping):
        budgets = selection.get("budgets")
    if not isinstance(budgets, Mapping):
        budgets = {key: selection[key] for key in _BUDGET_FIELDS if key in selection}
    blocks = []
    raw_blocks = payload.get("blocks")
    if isinstance(raw_blocks, list):
        for index, block in enumerate(raw_blocks):
            if not isinstance(block, Mapping):
                continue
            text = block.get("content", block.get("text"))
            blocks.append(
                {
                    "name": block.get("name", str(index)),
                    "chars": block.get("chars", len(text) if isinstance(text, str) else None),
                    "path": f"blocks.{index}",
                }
            )
    return {
        "captured": snapshot is not None,
        "capture_status": payload.get("capture_status", "recorded") if snapshot is not None else "not_recorded",
        "event_ref": snapshot.event_ref if snapshot is not None else None,
        "selection_event_ref": selection_event.event_ref if selection_event is not None else None,
        "selection": selection,
        "budgets": dict(budgets),
        "budget_source": "recorded" if budgets else "not_recorded",
        "blocks": blocks,
    }


def project_model_calls(events: Sequence[AgentEvent], *, turn_status: str = "running") -> list[dict[str, object]]:
    calls = []
    for group in _model_groups(events):
        request = group.get("model_request")
        response = group.get("model_response")
        event = request if request is not None else response
        if event is None:
            continue
        request_payload = load_event_payload(request) if request is not None else {}
        response_payload = load_event_payload(response) if response is not None else {}
        calls.append(
            {
                "request_id": request_payload.get("request_id", response_payload.get("request_id")),
                "request_event_ref": request.event_ref if request is not None else None,
                "response_event_ref": response.event_ref if response is not None else None,
                "attempt": event.attempt,
                "model": request_payload.get("model", response_payload.get("model", "")),
                "status": response.status
                if response is not None
                else "running"
                if turn_status == "running"
                else "not_recorded",
                "duration_ms": response.duration_ms if response is not None else None,
                "usage": normalize_usage(response_payload.get("usage")),
                "input_preview": _preview(request_payload.get("messages", request_payload))
                if request is not None
                else "",
                "output_preview": _preview(
                    response_payload.get("content")
                    or response_payload.get("tool_calls")
                    or response_payload.get("error")
                    or response_payload
                )
                if response is not None
                else "",
                "capture_status": request_payload.get("capture_status", "not_recorded"),
            }
        )
    return calls


def project_tool_calls(events: Sequence[AgentEvent], *, turn_status: str = "running") -> list[dict[str, object]]:
    groups: dict[str, dict[str, AgentEvent]] = {}
    audited = any(event.event_type in {"turn_timing", "message_delivery"} for event in events)
    deliveries: dict[str, list[dict[str, object]]] = {}
    for output in project_outputs(events):
        execution_ref = output.get("execution_ref")
        if output["source"] == "message_delivery" and isinstance(execution_ref, str) and execution_ref:
            deliveries.setdefault(execution_ref, []).append(output)
    for event in events:
        if event.event_type in {"assistant_tool_call", "tool_result"}:
            groups.setdefault(event.execution_ref or event.event_ref, {})[event.event_type] = event
    calls = []
    for group in groups.values():
        call = group.get("assistant_tool_call")
        result = group.get("tool_result")
        event = result if result is not None else call
        if event is None:
            continue
        call_payload = load_event_payload(call) if call is not None else {}
        result_payload = load_event_payload(result) if result is not None else {}
        audit_arguments = call_payload.get("audit_arguments")
        audit_result = result_payload.get("audit_result")
        arguments = (
            audit_arguments.get("data")
            if isinstance(audit_arguments, Mapping)
            else call_payload.get("arguments", call_payload)
        )
        result_value = (
            audit_result.get("data")
            if isinstance(audit_result, Mapping)
            else result_payload.get("result", result_payload)
        )
        calls.append(
            {
                "execution_ref": event.execution_ref or None,
                "tool_name": event.tool_name,
                "attempt": event.attempt,
                "call_event_ref": call.event_ref if call is not None else None,
                "result_event_ref": result.event_ref if result is not None else None,
                "status": result.status
                if result is not None
                else "running"
                if turn_status == "running"
                else "not_recorded",
                "effect": result.effect if result is not None else event.effect,
                "duration_ms": result.duration_ms if result is not None else None,
                "arguments_preview": _preview(arguments) if call is not None else "",
                "result_preview": _preview(result_value) if result is not None else "",
                "arguments_path": "audit_arguments.data"
                if isinstance(audit_arguments, Mapping)
                else "arguments"
                if "arguments" in call_payload
                else "",
                "result_path": "audit_result.data"
                if isinstance(audit_result, Mapping)
                else "result"
                if "result" in result_payload
                else "",
                "arguments_capture_status": audit_arguments.get("capture_status", "not_recorded")
                if isinstance(audit_arguments, Mapping)
                else "not_recorded",
                "result_capture_status": audit_result.get("capture_status", "not_recorded")
                if isinstance(audit_result, Mapping)
                else "not_recorded",
                "evidence": event_evidence(result_payload),
                "images": event_images(event, result_payload),
                "deliveries": deliveries.get(event.execution_ref, []),
                "delivery_source": "message_delivery" if audited else "legacy_incomplete",
            }
        )
    return calls


def project_outputs(events: Sequence[AgentEvent]) -> list[dict[str, object]]:
    """Prefer receipts, including a confirmed prefix when the rest of a turn failed."""
    ordered = sorted(events, key=lambda event: event.sequence or 0)
    audited = any(event.event_type in {"turn_timing", "message_delivery"} for event in ordered)
    outputs: list[dict[str, object]] = []
    for event in ordered:
        confirmed = event.event_type == "message_delivery" and event.status == event.effect == "confirmed"
        if audited and not confirmed:
            continue
        if not audited and not (
            (event.event_type == "assistant_output" and event.effect == "confirmed")
            or (event.event_type == "tool_result" and (event.effect == "confirmed" or event.status == "succeeded"))
        ):
            continue
        payload = load_event_payload(event)
        images = event_images(event, payload, output_only=True)
        if not audited and event.event_type == "tool_result" and not images:
            continue
        content = payload.get("content")
        media = payload.get("media")
        outputs.append(
            {
                "event_ref": event.event_ref,
                "event_type": event.event_type,
                "sequence": event.sequence,
                "execution_ref": event.execution_ref or None,
                "status": event.status,
                "effect": event.effect,
                "model_visible": False if confirmed else event.model_visible,
                "source": "message_delivery" if confirmed else "legacy_incomplete",
                "content": content if isinstance(content, str) else "",
                "preview": event_preview(event, payload),
                "images": images,
                "media": [
                    {key: value[key] for key in ("kind", "label", "capture_status") if isinstance(value.get(key), str)}
                    for value in media
                    if isinstance(value, Mapping)
                ]
                if isinstance(media, list)
                else [],
                "capture_status": payload.get("capture_status", "not_recorded") if confirmed else "legacy_incomplete",
                "received_at": payload.get("received_at") if confirmed else None,
                "confirmed_at": payload.get("confirmed_at") if confirmed else None,
                "duration_ms": event.duration_ms if confirmed else None,
            }
        )
    return outputs


async def turn_events(turn_id: int) -> list[AgentEvent]:
    async with get_session() as db:
        return list(
            (
                await db.execute(
                    select(AgentEvent).where(AgentEvent.turn_id == turn_id).order_by(AgentEvent.sequence.asc())
                )
            )
            .scalars()
            .all()
        )


async def session_usage(session_id: int) -> dict[str, object]:
    payload = type_coerce(AgentEvent.payload_json, JSON)
    async with get_session() as db:
        rows = (
            await db.execute(
                select(
                    AgentEvent.turn_id,
                    AgentEvent.event_ref,
                    AgentEvent.event_type,
                    AgentEvent.attempt,
                    payload["request_id"].as_string(),
                    payload["usage"],
                    payload["metrics"],
                )
                .join(AgentTurn, AgentTurn.id == AgentEvent.turn_id)
                .where(
                    AgentTurn.session_id == session_id,
                    AgentEvent.event_type.in_(("model_request", "model_response", "model_attempt")),
                )
                .order_by(AgentTurn.sequence.asc(), AgentEvent.sequence.asc())
            )
        ).all()
    return summarize_usage(
        [
            AgentEvent(
                turn_id=turn_id,
                event_ref=event_ref,
                event_type=event_type,
                attempt=attempt,
                payload_json=json.dumps({"request_id": request_id, "usage": usage, "metrics": metrics}),
            )
            for turn_id, event_ref, event_type, attempt, request_id, usage, metrics in rows
        ]
    )


async def session_context_summary(session_id: int) -> dict[str, object]:
    payload = type_coerce(AgentEvent.payload_json, JSON)
    events = []
    async with get_session() as db:
        latest_turn_id = (
            await db.execute(
                select(AgentEvent.turn_id)
                .join(AgentTurn, AgentTurn.id == AgentEvent.turn_id)
                .where(
                    AgentTurn.session_id == session_id,
                    AgentEvent.event_type.in_(("context_snapshot", "context_selection")),
                )
                .order_by(AgentTurn.sequence.desc(), AgentEvent.sequence.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if latest_turn_id is not None:
            rows = (
                await db.execute(
                    select(
                        AgentEvent.event_ref,
                        AgentEvent.event_type,
                        payload["selection"],
                        payload["budgets"],
                        payload["estimated_tokens"].as_integer(),
                        payload["included_turn_refs"],
                        payload["excluded_turn_refs"],
                        payload["max_input_tokens"].as_integer(),
                        payload["output_reserve_tokens"].as_integer(),
                    )
                    .where(
                        AgentEvent.turn_id == latest_turn_id,
                        AgentEvent.event_type.in_(("context_snapshot", "context_selection")),
                    )
                    .order_by(AgentEvent.sequence.asc())
                )
            ).all()
            for event_ref, kind, selected, recorded_budgets, estimate, included, excluded, maximum, reserve in rows:
                data = {
                    "selection": selected,
                    "budgets": recorded_budgets,
                    "estimated_tokens": estimate,
                    "included_turn_refs": included,
                    "excluded_turn_refs": excluded,
                }
                if maximum is not None:
                    data["max_input_tokens"] = maximum
                if reserve is not None:
                    data["output_reserve_tokens"] = reserve
                events.append(AgentEvent(event_ref=event_ref, event_type=kind, payload_json=json.dumps(data)))
    context = project_context(events)
    selection = context["selection"]
    budgets = context["budgets"]
    assert isinstance(selection, dict)
    assert isinstance(budgets, dict)
    included = selection.get("included_turn_refs")
    excluded = selection.get("excluded_turn_refs")
    return {
        "captured": context["captured"],
        "event_ref": context["event_ref"],
        "selection_event_ref": context["selection_event_ref"],
        "estimated_tokens": selection.get("estimated_tokens"),
        "max_input_tokens": budgets.get("max_input_tokens"),
        "output_reserve_tokens": budgets.get("output_reserve_tokens"),
        "included_count": len(included) if isinstance(included, list) else None,
        "excluded_count": len(excluded) if isinstance(excluded, list) else None,
        "budget_source": context["budget_source"],
    }
