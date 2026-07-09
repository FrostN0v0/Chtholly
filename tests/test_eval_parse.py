"""Unit tests for evaluator JSON parsing, clamping and delta application."""

from plugins.llm_chat.core.eval import (
    DELTA_LIMIT,
    MOOD_DELTA_LIMIT,
    apply_deltas,
    parse_eval_response,
)

VALID = '{"mood_delta": 0.1, "affection": 3, "trust": 1, "dependence": 0, "resentment": -2, "impression": "很友善"}'


class TestParse:
    def test_valid_json(self):
        result = parse_eval_response(VALID)
        assert result is not None
        assert result.mood_delta == 0.1
        assert result.deltas["affection"] == 3
        assert result.deltas["resentment"] == -2
        assert result.impression == "很友善"
        assert result.profile_patches == []
        assert result.memory_items == []

    def test_fenced_json_accepted(self):
        result = parse_eval_response(f"```json\n{VALID}\n```")
        assert result is not None
        assert result.deltas["affection"] == 3
        assert result.profile_patches == []
        assert result.memory_items == []

    def test_malformed_json_returns_none(self):
        assert parse_eval_response("not json at all") is None

    def test_non_dict_returns_none(self):
        assert parse_eval_response("[1, 2, 3]") is None

    def test_non_numeric_axis_returns_none(self):
        assert parse_eval_response('{"affection": "big", "trust": 0, "dependence": 0, "resentment": 0}') is None

    def test_bool_axis_rejected(self):
        assert parse_eval_response('{"affection": true, "trust": 0, "dependence": 0, "resentment": 0}') is None

    def test_missing_keys_default_to_zero(self):
        result = parse_eval_response('{"impression": "x"}')
        assert result is not None
        assert result.deltas == {"affection": 0.0, "trust": 0.0, "dependence": 0.0, "resentment": 0.0}
        assert result.mood_delta == 0.0
        assert result.profile_patches == []
        assert result.memory_items == []

    def test_out_of_range_deltas_clamped(self):
        payload = '{"mood_delta": 5, "affection": 100, "trust": -100, "dependence": 0, "resentment": 0}'
        result = parse_eval_response(payload)
        assert result is not None
        assert result.deltas["affection"] == DELTA_LIMIT
        assert result.deltas["trust"] == -DELTA_LIMIT
        assert result.mood_delta == MOOD_DELTA_LIMIT

    def test_impression_truncated_to_limit(self):
        long = "长" * 100
        result = parse_eval_response(f'{{"impression": "{long}"}}')
        assert result is not None
        assert len(result.impression) == 50

    def test_non_string_impression_falls_back(self):
        result = parse_eval_response('{"impression": 42}', current_impression="旧画像")
        assert result is not None
        assert result.impression == "旧画像"

    def test_profile_patches_and_memory_items_are_parsed(self):
        payload = (
            '{"profile_patches": ['
            '{"category": "preference", "key": "drink", "value": "tea", '
            '"confidence": 0.8, "evidence": "用户说喜欢茶"}'
            '], "memory_items": ['
            '{"text": "用户喜欢安静的早晨", "importance": 0.7}'
            "]}"
        )
        result = parse_eval_response(payload)

        assert result is not None
        assert len(result.profile_patches) == 1
        patch = result.profile_patches[0]
        assert patch.category == "preference"
        assert patch.key == "drink"
        assert patch.value == "tea"
        assert patch.confidence == 0.8
        assert patch.evidence == "用户说喜欢茶"
        assert len(result.memory_items) == 1
        memory = result.memory_items[0]
        assert memory.text == "用户喜欢安静的早晨"
        assert memory.importance == 0.7

    def test_low_confidence_profile_patch_is_skipped(self):
        payload = (
            '{"profile_patches": ['
            '{"category": "interest", "key": "music", "value": "jazz", '
            '"confidence": 0.7, "evidence": "提到一次爵士"}'
            "]}"
        )
        result = parse_eval_response(payload, min_profile_confidence=0.8)

        assert result is not None
        assert result.profile_patches == []

    def test_invalid_profile_category_is_skipped(self):
        payload = (
            '{"profile_patches": ['
            '{"category": "temporary_mood", "key": "mood", "value": "sleepy", '
            '"confidence": 0.9, "evidence": "临时说困"}'
            "]}"
        )
        result = parse_eval_response(payload)

        assert result is not None
        assert result.profile_patches == []


class TestApplyDeltas:
    def test_axes_stay_in_bounds(self):
        result = parse_eval_response('{"affection": 5, "trust": -5, "dependence": 0, "resentment": 0}')
        assert result is not None
        axes = {"affection": 98.0, "trust": 2.0, "dependence": 50.0, "resentment": 0.0}
        updated = apply_deltas(axes, result)
        assert updated["affection"] == 100.0  # clamped at ceiling
        assert updated["trust"] == 0.0  # clamped at floor
        assert updated["dependence"] == 50.0
