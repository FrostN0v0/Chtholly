"""Validation, serialization and continuous affect decay."""

from __future__ import annotations

import math
from typing import TypeGuard
from collections.abc import Mapping, Sequence

from .models import AXIS_KEYS, EmotionState, RelationshipEvaluation

_HALF_LIFE = 6 * 3600.0


def _num(value: object) -> TypeGuard[int | float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def read_emotions(value: object) -> tuple[EmotionState, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    out = []
    for raw in value:
        if not isinstance(raw, Mapping):
            continue
        name, intensity, cause, ids, ts = (
            raw.get(k) for k in ("name", "intensity", "cause", "evidence_turn_ids", "updated_at")
        )
        if (
            not isinstance(name, str)
            or not name.strip()
            or len(name.strip()) > 32
            or not _num(intensity)
            or not isinstance(cause, str)
            or len(cause.strip()) > 200
            or not isinstance(ids, (list, tuple))
            or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids)
            or not _num(ts)
        ):
            continue
        x = float(intensity)
        if x < 0 or x > 1:
            continue
        out.append(EmotionState(name.strip(), x, cause.strip(), tuple(ids), float(ts)))
    return tuple(out[:4])


def serialize_emotions(emotions: Sequence[EmotionState]) -> list[dict[str, object]]:
    return [
        {
            "name": e.name,
            "intensity": e.intensity,
            "cause": e.cause,
            "evidence_turn_ids": list(e.evidence_turn_ids),
            "updated_at": e.updated_at,
        }
        for e in emotions
    ]


def decay_emotions(emotions: Sequence[EmotionState], *, now: float) -> tuple[EmotionState, ...]:
    out = []
    for e in emotions:
        if not math.isfinite(now) or now < e.updated_at:
            out.append(e)
            continue
        intensity = e.intensity * 2 ** (-(now - e.updated_at) / _HALF_LIFE)
        if intensity >= 1e-3:
            out.append(EmotionState(e.name, intensity, e.cause, e.evidence_turn_ids, e.updated_at))
    return tuple(out[:4])


def parse_relationship_payload(
    payload: Mapping[str, object],
    *,
    expected_turn_ids: Sequence[int],
    previous_emotions: Sequence[EmotionState] = (),
    now: float,
) -> RelationshipEvaluation:
    if not isinstance(payload, Mapping):
        raise ValueError("payload must be an object")
    turn_ids = tuple(expected_turn_ids)
    if len(set(turn_ids)) != len(turn_ids):
        raise ValueError("expected turn ids must be unique")
    got = payload.get("processed_turn_ids")
    if (
        not isinstance(got, list)
        or tuple(got) != turn_ids
        or not all(isinstance(i, int) and not isinstance(i, bool) for i in got)
    ):
        raise ValueError("processed_turn_ids mismatch")
    rawd = payload.get("deltas")
    if not isinstance(rawd, Mapping):
        raise ValueError("deltas required")
    deltas = {}
    for key in AXIS_KEYS:
        value = rawd.get(key)
        if not _num(value) or abs(float(value)) > 100:
            raise ValueError("invalid delta")
        deltas[key] = float(value)
    desc, imp = payload.get("description"), payload.get("impression")
    if (
        not isinstance(desc, str)
        or not desc.strip()
        or len(desc.strip()) > 600
        or not isinstance(imp, str)
        or len(imp.strip()) > 160
    ):
        raise ValueError("invalid description")
    raw = payload.get("emotions")
    if not isinstance(raw, list) or len(raw) > 4:
        raise ValueError("invalid emotions")
    prior = decay_emotions(previous_emotions, now=now)
    allowed = set(turn_ids) | {i for emotion in previous_emotions for i in emotion.evidence_turn_ids}
    emotions = []
    for item in raw:
        if not isinstance(item, Mapping) or set(item) - {"name", "intensity", "cause", "evidence_turn_ids"}:
            raise ValueError("invalid emotion")
        name, intensity, cause, evidence = (item.get(k) for k in ("name", "intensity", "cause", "evidence_turn_ids"))
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 32 or not _num(intensity):
            raise ValueError("invalid emotion name or intensity")
        if not isinstance(cause, str) or not cause.strip() or len(cause.strip()) > 200:
            raise ValueError("invalid emotion cause")
        if not isinstance(evidence, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in evidence):
            raise ValueError("invalid emotion evidence")
        if len(set(evidence)) != len(evidence) or not set(evidence) <= allowed:
            raise ValueError("invalid emotion evidence")
        if not 0 <= float(intensity) <= 1 or (float(intensity) > 0 and not evidence):
            raise ValueError("invalid emotion intensity or missing evidence")
        old = next(
            (
                e
                for e in previous_emotions
                if e.name == name.strip() and e.cause == cause.strip() and tuple(e.evidence_turn_ids) == tuple(evidence)
            ),
            None,
        )
        effective = next(
            (
                e
                for e in prior
                if e.name == name.strip() and e.cause == cause.strip() and tuple(e.evidence_turn_ids) == tuple(evidence)
            ),
            None,
        )
        if old is not None and (
            math.isclose(old.intensity, float(intensity), abs_tol=0.001)
            or (effective is not None and math.isclose(effective.intensity, float(intensity), abs_tol=0.001))
        ):
            emotions.append(old)
            continue
        emotions.append(EmotionState(name.strip(), float(intensity), cause.strip(), tuple(evidence), now))
    return RelationshipEvaluation(deltas, desc.strip(), imp.strip(), tuple(emotions), turn_ids)


def build_relationship_context(
    *, axes: Mapping[str, float], description: str, impression: str, emotions: Sequence[EmotionState], now: float
) -> dict[str, object]:
    return {
        "axes": {k: float(axes.get(k, 0.0)) for k in AXIS_KEYS},
        "description": description,
        "impression": impression,
        "emotions": [
            {"name": e.name, "intensity": e.intensity, "cause": e.cause} for e in decay_emotions(emotions, now=now)
        ],
    }
