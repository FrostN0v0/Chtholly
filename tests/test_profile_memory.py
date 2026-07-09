"""Unit tests for pure profile merging and semantic-memory ranking helpers."""

import pytest

from utils.llm_chat_core.profile import (
    REPLACE_MARGIN,
    REINFORCE_BONUS,
    CONFLICT_PENALTY,
    ProfilePatch,
    ProfileFactSnapshot,
    fact_rank_key,
    cosine_similarity,
    rank_by_similarity,
    match_duplicate_memory,
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

    def test_values_match_true_reinforces_despite_different_value_strings(self):
        existing = ProfileFactSnapshot(value="tea", confidence=0.7, evidence_count=2)

        merged = merge_profile_snapshot(existing, _patch("green tea", 0.6), values_match=True)

        assert merged.value == "tea"
        assert merged.confidence == pytest.approx(0.7 + REINFORCE_BONUS)
        assert merged.evidence_count == 3

    def test_values_match_true_reinforcement_caps_at_one(self):
        existing = ProfileFactSnapshot(value="tea", confidence=0.95, evidence_count=2)

        merged = merge_profile_snapshot(existing, _patch("herbal tea", 0.9), values_match=True)

        assert merged.value == "tea"
        assert merged.confidence == 1.0
        assert merged.evidence_count == 3

    def test_values_match_false_penalizes_identical_value_strings(self):
        existing = ProfileFactSnapshot(value="tea", confidence=0.8, evidence_count=4)

        merged = merge_profile_snapshot(existing, _patch("tea", 0.7), values_match=False)

        assert merged.value == "tea"
        assert merged.confidence == pytest.approx(0.8 - CONFLICT_PENALTY)
        assert merged.evidence_count == 5

    def test_values_match_false_strong_patch_replaces_with_patch_confidence(self):
        existing = ProfileFactSnapshot(value="tea", confidence=0.6, evidence_count=4)

        merged = merge_profile_snapshot(existing, _patch("tea", 0.6 + REPLACE_MARGIN), values_match=False)

        assert merged.value == "tea"
        assert merged.confidence == pytest.approx(0.6 + REPLACE_MARGIN)
        assert merged.evidence_count == 5

    def test_values_match_ignored_when_existing_is_none(self):
        patch = _patch("tea", 0.7)

        merged = merge_profile_snapshot(None, patch, values_match=False)

        assert merged.value == "tea"
        assert merged.confidence == 0.7
        assert merged.evidence_count == 1


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


class TestMatchDuplicateMemory:
    def test_exact_text_match_returns_candidate_id_without_embeddings(self):
        match = match_duplicate_memory(
            None,
            "likes tea",
            [(7, "likes tea", None)],
            min_similarity=1.0,
        )

        assert match == 7

    def test_semantic_match_respects_min_similarity_and_rejects_orthogonal_candidate(self):
        match = match_duplicate_memory(
            [1, 0],
            "likes tea",
            [(8, "prefers tea", [1, 0])],
            min_similarity=0.92,
        )
        miss = match_duplicate_memory(
            [1, 0],
            "likes tea",
            [(9, "prefers coffee", [0, 1])],
            min_similarity=0.92,
        )

        assert match == 8
        assert miss is None

    def test_semantic_match_includes_exact_threshold_boundary(self):
        match = match_duplicate_memory(
            [1, 0],
            "likes tea",
            [(10, "prefers tea", [1, 0])],
            min_similarity=1.0,
        )

        assert match == 10

    def test_missing_embeddings_do_not_match_without_exact_text(self):
        missing_query_embedding = match_duplicate_memory(
            None,
            "likes tea",
            [(11, "prefers tea", [1, 0])],
            min_similarity=0.5,
        )
        missing_candidate_embedding = match_duplicate_memory(
            [1, 0],
            "likes tea",
            [(12, "prefers tea", None)],
            min_similarity=0.5,
        )

        assert missing_query_embedding is None
        assert missing_candidate_embedding is None

    def test_best_scoring_candidate_wins_and_ties_keep_first_candidate(self):
        best_match = match_duplicate_memory(
            [1, 0],
            "likes tea",
            [
                (13, "prefers green tea", [0.8, 0.6]),
                (14, "prefers black tea", [1, 0]),
            ],
            min_similarity=0.7,
        )
        first_tied_match = match_duplicate_memory(
            [1, 0],
            "likes tea",
            [
                (15, "prefers green tea", [1, 0]),
                (16, "prefers black tea", [1, 0]),
            ],
            min_similarity=0.7,
        )

        assert best_match == 14
        assert first_tied_match == 15


class TestFactRankKey:
    def test_missing_query_embedding_uses_confidence_then_evidence_count(self):
        ranked = sorted(
            [
                ("more_evidence", fact_rank_key(None, "[1,0]", 0.8, 4)),
                ("higher_confidence", fact_rank_key(None, "[0,1]", 0.9, 1)),
                ("same_confidence_more_evidence", fact_rank_key(None, "[-1,0]", 0.8, 5)),
            ],
            key=lambda item: item[1],
            reverse=True,
        )

        assert fact_rank_key(None, "[1,0]", 0.8, 4) == (0.0, 0.8, 4)
        assert [name for name, _key in ranked] == [
            "higher_confidence",
            "same_confidence_more_evidence",
            "more_evidence",
        ]

    @pytest.mark.parametrize("embedding_json", ["", "not json"])
    def test_invalid_embedding_json_scores_zero(self, embedding_json):
        score, confidence, evidence_count = fact_rank_key([1, 0], embedding_json, 0.7, 2)

        assert score == 0.0
        assert confidence == 0.7
        assert evidence_count == 2

    @pytest.mark.parametrize(
        ("embedding_json", "expected_score"),
        [
            ("[1,0]", 1.0),
            ("[0,1]", 0.0),
            ("[-1,0]", -1.0),
        ],
    )
    def test_valid_embedding_json_scores_by_cosine_similarity(self, embedding_json, expected_score):
        score, confidence, evidence_count = fact_rank_key([1, 0], embedding_json, 0.6, 3)

        assert score == pytest.approx(expected_score)
        assert confidence == 0.6
        assert evidence_count == 3

    def test_query_embedding_makes_relevant_fact_outrank_higher_confidence_fact(self):
        facts = [
            ("relevant_lower_confidence", "[1,0]", 0.4, 1),
            ("irrelevant_higher_confidence", "[0,1]", 0.9, 5),
        ]

        ranked_with_query = sorted(
            facts,
            key=lambda fact: fact_rank_key([1, 0], fact[1], fact[2], fact[3]),
            reverse=True,
        )
        ranked_without_query = sorted(
            facts,
            key=lambda fact: fact_rank_key(None, fact[1], fact[2], fact[3]),
            reverse=True,
        )

        assert [fact[0] for fact in ranked_with_query] == [
            "relevant_lower_confidence",
            "irrelevant_higher_confidence",
        ]
        assert [fact[0] for fact in ranked_without_query] == [
            "irrelevant_higher_confidence",
            "relevant_lower_confidence",
        ]
