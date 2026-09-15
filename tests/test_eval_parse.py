"""Behavioral tests for batch relationship evidence parsing."""

import json

import pytest

from utils.relationship_core import EmotionState, decay_emotions
from plugins.llm_chat.core.eval import parse_eval_response


def payload(ids=(1, 2), **extra):
    value = {
        "deltas": {"affection": 2, "trust": 0, "dependence": 0, "resentment": -1, "familiarity": 1},
        "description": "steady relationship",
        "impression": "thoughtful",
        "emotions": [{"name": "mixed", "intensity": 0.7, "cause": "care with caution", "evidence_turn_ids": [1]}],
        "processed_turn_ids": list(ids),
        "profile_patches": [],
        "memory_items": [],
    }
    value.update(extra)
    return value


def test_batch_parser_accepts_model_emotion_without_timestamp():
    result = parse_eval_response(json.dumps(payload()), expected_turn_ids=(1, 2), now=100.0)
    assert result is not None
    assert result.processed_turn_ids == (1, 2)
    assert result.emotions[0].updated_at == 100.0
    assert result.emotions[0].evidence_turn_ids == (1,)


def test_invalid_batch_or_evidence_cannot_clear_state():
    previous = (EmotionState("care", 0.8, "prior", (9,), 10.0),)
    assert (
        parse_eval_response(
            json.dumps(payload(processed_turn_ids=[1])), expected_turn_ids=(1, 2), previous_emotions=previous, now=20
        )
        is None
    )
    assert (
        parse_eval_response(
            json.dumps(payload(emotions=[{"name": "x", "intensity": 0.5, "cause": "bad", "evidence_turn_ids": [999]}])),
            expected_turn_ids=(1, 2),
            previous_emotions=previous,
            now=20,
        )
        is None
    )


def test_nonfinite_values_rejected():
    result = parse_eval_response(
        json.dumps(
            payload(deltas={"affection": float("nan"), "trust": 0, "dependence": 0, "resentment": 0, "familiarity": 0})
        ),
        expected_turn_ids=(1, 2),
        now=0,
    )
    assert result is None


def test_unchanged_effective_emotion_preserves_original_storage_timestamp():
    from utils.relationship_core import EmotionState

    previous = (EmotionState("joy", 1.0, "event", (1,), 0.0),)
    result = parse_eval_response(
        json.dumps(payload(emotions=[{"name": "joy", "intensity": 0.5, "cause": "event", "evidence_turn_ids": [1]}])),
        expected_turn_ids=(1, 2),
        previous_emotions=previous,
        now=21600.0,
    )
    assert result is not None
    assert result.emotions[0] == previous[0]


def test_empty_emotions_is_valid_settled_state():
    result = parse_eval_response(json.dumps(payload(emotions=[])), expected_turn_ids=(1, 2), now=0)
    assert result is not None
    assert result.emotions == ()


def test_decay_uses_elapsed_time_without_mutating_stored_emotion():
    emotion = EmotionState("joy", 1.0, "event", (1,), 0.0)
    effective = decay_emotions((emotion,), now=21600.0)
    assert effective[0].intensity == pytest.approx(0.5)
    assert effective[0].updated_at == 0.0
    assert decay_emotions((emotion,), now=43200.0)[0].intensity == pytest.approx(0.25)
    assert decay_emotions((emotion,), now=21600.0) == effective


@pytest.mark.parametrize(("intensity", "expired"), [(0.8, False), (0.0011, True)])
def test_echoed_emotion_preserves_decay_during_model_latency(intensity, expired):
    previous = (EmotionState("care", intensity, "Earlier support", (9,), 100.0),)
    output = payload(
        ids=(10,),
        emotions=[
            {
                "name": "care",
                "intensity": intensity,
                "cause": "Earlier support",
                "evidence_turn_ids": [9],
            }
        ],
    )
    result = parse_eval_response(
        json.dumps(output),
        expected_turn_ids=(10,),
        previous_emotions=previous,
        now=21700.0,
    )
    assert result is not None
    after = decay_emotions(result.emotions, now=21700.0)
    if expired:
        assert after == ()
    else:
        assert after[0].intensity == pytest.approx(0.4)
        assert after[0].updated_at == 100.0
