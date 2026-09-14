"""Administrator-only relationship evaluation event projection and updates."""

from __future__ import annotations

import json
from typing import cast
from datetime import datetime
from collections.abc import Mapping, Sequence

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from .types import EvaluationBatch
from ..models import AgentEvent
from ..core.types import JSONType


def utc_text(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds") + "Z"


def pending_payload(*, turn_id: int, model: str, queued_at: datetime) -> dict[str, JSONType]:
    return {
        "evaluation_ref": "",
        "evidence_turn_ids": [turn_id],
        "before": None,
        "after": None,
        "model": model,
        "error": None,
        "started_at": None,
        "finished_at": None,
        "queued_at": utc_text(queued_at),
        "changes": {},
    }


def evaluation_payload(
    batch: EvaluationBatch,
    *,
    after: Mapping[str, JSONType] | None = None,
    error: str | None = None,
    finished_at: datetime | None = None,
) -> dict[str, JSONType]:
    changes: dict[str, JSONType] = {}
    before_axes = batch.before.get("axes")
    after_axes = after.get("axes") if after is not None else None
    if isinstance(before_axes, Mapping) and isinstance(after_axes, Mapping):
        for axis, value in before_axes.items():
            new_value = after_axes.get(axis)
            if isinstance(value, (float, int)) and isinstance(new_value, (float, int)):
                changes[axis] = {"before": value, "after": new_value, "delta": round(new_value - value, 3)}
    return {
        "evaluation_ref": batch.evaluation_ref,
        "evidence_turn_ids": list(batch.turn_ids),
        "before": batch.before,
        "after": cast(JSONType, dict(after)) if after is not None else None,
        "model": batch.model,
        "error": error,
        "started_at": utc_text(batch.started_at),
        "finished_at": utc_text(finished_at) if finished_at is not None else None,
        "queued_at": batch.queued_at,
        "changes": changes,
    }


async def update_evaluation_events(
    db: AsyncSession,
    event_ids: Sequence[int],
    *,
    status: str,
    payload: Mapping[str, JSONType],
    duration_ms: int = 0,
) -> None:
    await db.execute(
        update(AgentEvent)
        .where(AgentEvent.id.in_(event_ids), AgentEvent.event_type == "relationship_evaluation")
        .values(
            status=status,
            effect="state_updated" if status == "succeeded" else "none",
            payload_json=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            duration_ms=max(0, duration_ms),
            model_visible=False,
        )
    )
