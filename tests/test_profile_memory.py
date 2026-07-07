"""Unit tests for pure profile merging and semantic-memory ranking helpers."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from llm_chat_src.persona.profile import (  # noqa: E402
    REPLACE_MARGIN,
    REINFORCE_BONUS,
    CONFLICT_PENALTY,
    ProfilePatch,
    ProfileFactSnapshot,
    cosine_similarity,
    rank_by_similarity,
    merge_profile_snapshot,
)


def _patch(value: str, confidence: float) -> ProfilePatch:
    return ProfilePatch(
        category="preference",
        key="drink",
        value=value,
        confidence=confidence,
        evidence="用户明确提到饮品偏好",
    )


class TestMergeProfileSnapshot:
    def test_new_patch_creates_one_evidence_fact(self):
        patch = _patch("tea", 0.7)

        merged = merge_profile_snapshot(None, patch)

        assert merged.value == "tea"
        assert merged.confidence == 0.7
        assert merged.evidence_count == 1

    def test_repeated_value_reinforces_confidence_and_evidence_count(self):
        existing = ProfileFactSnapshot(value="tea", confidence=0.7, evidence_count=2)

        merged = merge_profile_snapshot(existing, _patch("tea", 0.8))

        assert merged.value == "tea"
        assert merged.confidence == pytest.approx(0.8 + REINFORCE_BONUS)
        assert merged.evidence_count == 3

    def test_repeated_value_reinforcement_caps_at_one(self):
        existing = ProfileFactSnapshot(value="tea", confidence=0.95, evidence_count=2)

        merged = merge_profile_snapshot(existing, _patch("tea", 0.9))

        assert merged.value == "tea"
        assert merged.confidence == 1.0
        assert merged.evidence_count == 3

    def test_weak_conflict_keeps_existing_value_and_lowers_confidence(self):
        existing = ProfileFactSnapshot(value="tea", confidence=0.85, evidence_count=4)

        merged = merge_profile_snapshot(existing, _patch("coffee", 0.7))

        assert merged.value == "tea"
        assert merged.confidence == pytest.approx(0.85 - CONFLICT_PENALTY)
        assert merged.evidence_count == 5

    def test_strong_conflict_replaces_existing_value(self):
        existing = ProfileFactSnapshot(value="tea", confidence=0.6, evidence_count=4)

        merged = merge_profile_snapshot(existing, _patch("coffee", 0.6 + REPLACE_MARGIN))

        assert merged.value == "coffee"
        assert merged.confidence == pytest.approx(0.6 + REPLACE_MARGIN)
        assert merged.evidence_count == 5


class TestSimilarityRanking:
    def test_cosine_similarity_identity_and_mismatched_lengths(self):
        assert cosine_similarity([1, 0], [1, 0]) == 1.0
        assert cosine_similarity([1, 0], [1, 0, 0]) == 0.0

    def test_rank_by_similarity_respects_limit_and_min_similarity(self):
        ranked = rank_by_similarity(
            [1, 0],
            [
                ("diagonal", [1, 1]),
                ("orthogonal", [0, 1]),
                ("exact", [1, 0]),
                ("opposite", [-1, 0]),
            ],
            limit=2,
            min_similarity=0.7,
        )

        assert [text for text, _score in ranked] == ["exact", "diagonal"]
        assert [score for _text, score in ranked] == pytest.approx([1.0, 2**-0.5])
