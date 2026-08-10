"""Unit tests for pure profile merging and memory selection policies."""

from copy import deepcopy
from math import cos, sin, sqrt, radians
from datetime import datetime, timezone, timedelta

import pytest

from plugins.llm_chat.core.profile import (
    REPLACE_MARGIN,
    REINFORCE_BONUS,
    CONFLICT_PENALTY,
    ProfilePatch,
    ProfileFactSnapshot,
    cosine_similarity,
    match_duplicate_memory,
    merge_profile_snapshot,
)
from plugins.llm_chat.core.memory_policy import (
    MemoryCandidate,
    ProfileFactCandidate,
    group_profile_facts,
    normalize_prompt_text,
    profile_retrieval_score,
    select_relevant_memories,
    select_chat_profile_facts,
    select_admitted_memory_indexes,
)


def _patch(value: str, confidence: float) -> ProfilePatch:
    return ProfilePatch(
        category="preference",
        key="drink",
        value=value,
        confidence=confidence,
        evidence="用户明确提到饮品偏好",
    )


def _unit_vector(similarity: float, *, sign: float = 1.0) -> list[float]:
    return [similarity, sign * sqrt(max(0.0, 1.0 - similarity**2))]


def _fact(
    fact_id: int,
    category: str = "preference",
    key: str | None = None,
    value: str | None = None,
    *,
    confidence: float = 0.8,
    evidence_count: int = 1,
    updated_at: datetime | None = None,
    embedding: list[float] | None = None,
) -> ProfileFactCandidate:
    return ProfileFactCandidate(
        id=fact_id,
        category=category,
        key=key if key is not None else f"key_{fact_id}",
        value=value if value is not None else f"value_{fact_id}",
        confidence=confidence,
        evidence_count=evidence_count,
        updated_at=updated_at,
        embedding=embedding,
    )


def _memory(
    memory_id: int,
    *,
    text: str | None = None,
    importance: float = 0.8,
    created_at: datetime | None = None,
    embedding: list[float] | None = None,
) -> MemoryCandidate:
    return MemoryCandidate(
        id=memory_id,
        text=text if text is not None else f"memory {memory_id}",
        importance=importance,
        created_at=created_at,
        embedding=embedding,
    )


class TestMergeProfileSnapshot:
    def test_reinforce_bonus_contract_is_five_hundredths(self):
        assert REINFORCE_BONUS == 0.05

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


class TestCosineSimilarity:
    @pytest.mark.parametrize(
        ("left", "right", "expected"),
        [
            ([1, 0], [1, 0], 1.0),
            ([1, 0], [-1, 0], -1.0),
            ([1, 0], [0, 1], 0.0),
            ([1, 0], [1, 0, 0], 0.0),
            ([], [1, 0], 0.0),
            ([0, 0], [1, 0], 0.0),
        ],
    )
    def test_similarity_boundaries(self, left, right, expected):
        assert cosine_similarity(left, right) == pytest.approx(expected)


class TestNormalizePromptText:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("  green\t tea\nlover  ", "green tea lover"),
            ("Tea:  Green\r\nTEA", "Tea: Green TEA"),
            ("single", "single"),
            (" \n\t ", ""),
        ],
    )
    def test_only_collapses_whitespace(self, raw, expected):
        assert normalize_prompt_text(raw) == expected


class TestGroupProfileFacts:
    def test_exact_normalized_category_key_value_tuple_groups_without_embeddings(self):
        facts = [
            _fact(
                2,
                key="favorite drink",
                value="green tea",
                confidence=0.9,
                evidence_count=2,
            ),
            _fact(
                1,
                key=" favorite   drink ",
                value=" green\ntea ",
                confidence=0.7,
                embedding=None,
            ),
        ]

        grouped = group_profile_facts(facts, min_similarity=0.88)

        assert [fact.id for fact in grouped] == [2]
        assert grouped[0].key == "favorite drink"
        assert grouped[0].alias_keys == (" favorite   drink ",)

    def test_tuple_identity_does_not_merge_colon_collisions(self):
        facts = [
            _fact(1, key="a:b", value="c"),
            _fact(2, key="a", value="b:c"),
        ]

        grouped = group_profile_facts(facts, min_similarity=0.88)

        assert [fact.id for fact in grouped] == [1, 2]

    def test_semantic_merge_includes_088_and_rejects_just_below(self):
        facts = [
            _fact(10, key="canonical", confidence=0.9, embedding=[1.0, 0.0]),
            _fact(
                11,
                key="at_threshold",
                confidence=0.8,
                embedding=_unit_vector(0.88),
            ),
            _fact(
                12,
                key="below_threshold",
                confidence=0.7,
                embedding=_unit_vector(0.8799),
            ),
        ]

        grouped = group_profile_facts(facts, min_similarity=0.88)

        assert [fact.id for fact in grouped] == [10, 12]
        assert grouped[0].alias_keys == ("at_threshold",)
        assert grouped[1].alias_keys == ()

    def test_missing_empty_zero_and_mismatched_embeddings_do_not_semantically_merge(self):
        facts = [
            _fact(1, embedding=[1.0, 0.0]),
            _fact(2, embedding=None),
            _fact(3, embedding=[]),
            _fact(4, embedding=[1.0, 0.0, 0.0]),
            _fact(5, embedding=[0.0, 0.0]),
        ]

        grouped = group_profile_facts(facts, min_similarity=0.88)

        assert [fact.id for fact in grouped] == [1, 2, 3, 4, 5]

    def test_identical_embeddings_never_merge_across_categories(self):
        facts = [
            _fact(1, "preference", embedding=[1.0, 0.0]),
            _fact(2, "interest", embedding=[1.0, 0.0]),
        ]

        grouped = group_profile_facts(facts, min_similarity=0.88)

        assert [fact.id for fact in grouped] == [1, 2]

    def test_same_value_with_different_keys_does_not_merge_without_embeddings(self):
        facts = [
            _fact(1, key="drink", value="tea", embedding=None),
            _fact(2, key="beverage", value="tea", embedding=None),
        ]

        grouped = group_profile_facts(facts, min_similarity=0.88)

        assert [fact.id for fact in grouped] == [1, 2]

    def test_representative_greedy_grouping_is_non_transitive(self):
        vector_a = [1.0, 0.0]
        vector_b = [cos(radians(20)), sin(radians(20))]
        vector_c = [cos(radians(40)), sin(radians(40))]
        assert cosine_similarity(vector_a, vector_b) >= 0.88
        assert cosine_similarity(vector_b, vector_c) >= 0.88
        assert cosine_similarity(vector_a, vector_c) < 0.88
        facts = [
            _fact(1, key="a", confidence=0.95, embedding=vector_a),
            _fact(2, key="b", confidence=0.90, embedding=vector_b),
            _fact(3, key="c", confidence=0.85, embedding=vector_c),
        ]

        grouped = group_profile_facts(facts, min_similarity=0.88)

        assert [fact.id for fact in grouped] == [1, 3]
        assert grouped[0].alias_keys == ("b",)
        assert grouped[1].alias_keys == ()

    def test_canonical_rank_uses_confidence_evidence_time_then_lower_id(self):
        older = datetime(2025, 1, 1, tzinfo=timezone.utc)
        newer = older + timedelta(days=1)
        scenarios = [
            (
                [
                    _fact(20, confidence=0.9, evidence_count=1, updated_at=older, embedding=[1, 0]),
                    _fact(10, confidence=0.8, evidence_count=5, updated_at=newer, embedding=[1, 0]),
                ],
                20,
            ),
            (
                [
                    _fact(20, confidence=0.9, evidence_count=4, updated_at=older, embedding=[1, 0]),
                    _fact(10, confidence=0.9, evidence_count=3, updated_at=newer, embedding=[1, 0]),
                ],
                20,
            ),
            (
                [
                    _fact(20, confidence=0.9, evidence_count=4, updated_at=newer, embedding=[1, 0]),
                    _fact(10, confidence=0.9, evidence_count=4, updated_at=older, embedding=[1, 0]),
                ],
                20,
            ),
            (
                [
                    _fact(20, confidence=0.9, evidence_count=4, updated_at=newer, embedding=[1, 0]),
                    _fact(10, confidence=0.9, evidence_count=4, updated_at=newer, embedding=[1, 0]),
                ],
                10,
            ),
        ]

        for facts, expected_id in scenarios:
            grouped = group_profile_facts(facts, min_similarity=0.88)
            assert [fact.id for fact in grouped] == [expected_id]

    def test_aliases_are_unique_sorted_and_inputs_are_not_mutated(self):
        facts = [
            _fact(4, key="canonical", confidence=0.9, evidence_count=3, embedding=[1, 0]),
            _fact(1, key="zeta", confidence=0.8, embedding=[1, 0]),
            _fact(2, key="alpha", confidence=0.8, embedding=[1, 0]),
            _fact(3, key="zeta", confidence=0.7, embedding=[1, 0]),
        ]
        original = deepcopy(facts)

        grouped = group_profile_facts(facts, min_similarity=0.88)

        assert len(grouped) == 1
        assert grouped[0].id == 4
        assert grouped[0].alias_keys == ("alpha", "zeta")
        assert facts == original
        assert all(fact.alias_keys == () for fact in facts)

    def test_groups_are_returned_by_representative_id(self):
        facts = [
            _fact(20, confidence=0.9, embedding=[1, 0]),
            _fact(5, confidence=0.8, embedding=[0, 1]),
        ]

        grouped = group_profile_facts(facts, min_similarity=0.99)

        assert [fact.id for fact in grouped] == [5, 20]

    def test_candidate_joins_highest_similarity_group_and_ties_choose_first_group(self):
        closest_facts = [
            _fact(1, key="first", confidence=0.9, embedding=[1, 0]),
            _fact(2, key="second", confidence=0.8, embedding=[0, 1]),
            _fact(3, key="closest", confidence=0.7, embedding=_unit_vector(0.65)),
        ]
        tied_facts = [
            _fact(1, key="first", confidence=0.9, embedding=[1, 0]),
            _fact(2, key="second", confidence=0.8, embedding=[0, 1]),
            _fact(3, key="tied", confidence=0.7, embedding=[sqrt(0.5), sqrt(0.5)]),
        ]

        closest = group_profile_facts(closest_facts, min_similarity=0.60)
        tied = group_profile_facts(tied_facts, min_similarity=0.70)

        assert [fact.alias_keys for fact in closest] == [(), ("closest",)]
        assert [fact.alias_keys for fact in tied] == [("tied",), ()]


class TestMatchDuplicateMemory:
    def test_exact_text_match_returns_candidate_id_without_embeddings(self):
        match = match_duplicate_memory(
            None,
            "likes tea",
            [(7, "likes tea", None)],
            min_similarity=1.0,
        )

        assert match == 7

    def test_semantic_match_includes_088_and_rejects_just_below(self):
        at_threshold = match_duplicate_memory(
            [1.0, 0.0],
            "likes tea",
            [(8, "prefers tea", _unit_vector(0.88))],
            min_similarity=0.88,
        )
        below_threshold = match_duplicate_memory(
            [1.0, 0.0],
            "likes tea",
            [(9, "prefers tea", _unit_vector(0.8799))],
            min_similarity=0.88,
        )

        assert at_threshold == 8
        assert below_threshold is None

    def test_missing_embeddings_do_not_match_without_exact_text(self):
        missing_query_embedding = match_duplicate_memory(
            None,
            "likes tea",
            [(11, "prefers tea", [1, 0])],
            min_similarity=0.88,
        )
        missing_candidate_embedding = match_duplicate_memory(
            [1, 0],
            "likes tea",
            [(12, "prefers tea", None)],
            min_similarity=0.88,
        )

        assert missing_query_embedding is None
        assert missing_candidate_embedding is None

    def test_best_scoring_candidate_wins_and_ties_keep_first_candidate(self):
        best_match = match_duplicate_memory(
            [1, 0],
            "likes tea",
            [
                (13, "prefers green tea", _unit_vector(0.90)),
                (14, "prefers black tea", [1, 0]),
            ],
            min_similarity=0.88,
        )
        first_tied_match = match_duplicate_memory(
            [1, 0],
            "likes tea",
            [
                (15, "prefers green tea", [1, 0]),
                (16, "prefers black tea", [1, 0]),
            ],
            min_similarity=0.88,
        )

        assert best_match == 14
        assert first_tied_match == 15


class TestProfileRetrievalScore:
    def test_uses_weighted_similarity_confidence_and_capped_evidence(self):
        fact = _fact(
            1,
            confidence=0.5,
            evidence_count=9,
            embedding=_unit_vector(0.8),
        )

        score = profile_retrieval_score([1, 0], fact)

        assert score == pytest.approx(0.70 * 0.8 + 0.20 * 0.5 + 0.10)

    @pytest.mark.parametrize("query_embedding", [None, [1, 0, 0]])
    def test_missing_or_invalid_query_similarity_contributes_zero(self, query_embedding):
        fact = _fact(1, confidence=0.75, evidence_count=2, embedding=[1, 0])

        score = profile_retrieval_score(query_embedding, fact)

        assert score == pytest.approx(0.20 * 0.75 + 0.10 * 2 / 5)


class TestSelectChatProfileFacts:
    def test_enforces_boundary_and_fixed_2_2_1_1_bucket_order(self):
        facts = [
            _fact(1, "communication_style", confidence=0.99, evidence_count=5, embedding=[1, 0]),
            _fact(2, "communication_style", confidence=0.98, evidence_count=5, embedding=[1, 0]),
            _fact(3, "boundary", confidence=0.10, evidence_count=1, embedding=[0, 1]),
            _fact(4, "preference", confidence=0.80, evidence_count=1, embedding=[1, 0]),
            _fact(5, "interest", confidence=0.90, evidence_count=1, embedding=_unit_vector(0.9)),
            _fact(6, "preference", confidence=0.99, evidence_count=5, embedding=[0, 1]),
            _fact(7, "background", confidence=0.99, evidence_count=5, embedding=[0, 1]),
            _fact(8, "trait", confidence=0.80, evidence_count=2, embedding=[1, 0]),
            _fact(9, "trait", confidence=0.7999, evidence_count=5, embedding=[1, 0]),
            _fact(10, "trait", confidence=1.0, evidence_count=1, embedding=[1, 0]),
            _fact(11, "relationship", confidence=0.70, evidence_count=1, embedding=[1, 0]),
            _fact(12, "relationship", confidence=1.0, evidence_count=5, embedding=[0, 1]),
            _fact(13, "unknown", confidence=1.0, evidence_count=5, embedding=[1, 0]),
        ]

        selected = select_chat_profile_facts(facts, [1, 0], limit=6)

        assert [fact.id for fact in selected] == [3, 1, 4, 5, 8, 11]
        assert [fact.category for fact in selected] == [
            "boundary",
            "communication_style",
            "preference",
            "interest",
            "trait",
            "relationship",
        ]

    def test_trait_thresholds_are_inclusive_and_background_competes_for_one_slot(self):
        facts = [
            _fact(1, "trait", confidence=0.80, evidence_count=2, embedding=[1, 0]),
            _fact(2, "trait", confidence=0.7999, evidence_count=5, embedding=[1, 0]),
            _fact(3, "trait", confidence=1.0, evidence_count=1, embedding=[1, 0]),
            _fact(4, "background", confidence=0.9, evidence_count=5, embedding=[0, 1]),
        ]

        selected = select_chat_profile_facts(facts, [1, 0], limit=6)

        assert [fact.id for fact in selected] == [1]

    def test_limit_truncates_fixed_bucket_output(self):
        facts = [
            _fact(1, "boundary", embedding=[1, 0]),
            _fact(2, "communication_style", embedding=[1, 0]),
            _fact(3, "preference", embedding=[1, 0]),
            _fact(4, "interest", embedding=[1, 0]),
            _fact(5, "background", embedding=[1, 0]),
            _fact(6, "relationship", embedding=[1, 0]),
        ]

        selected = select_chat_profile_facts(facts, [1, 0], limit=4)

        assert [fact.id for fact in selected] == [1, 2, 3, 4]

    def test_empty_buckets_are_not_refilled_from_other_categories(self):
        facts = [_fact(index, "communication_style", confidence=1.0, embedding=[1, 0]) for index in range(1, 6)]

        selected = select_chat_profile_facts(facts, [1, 0], limit=6)

        assert [fact.id for fact in selected] == [1, 2]

    def test_missing_query_ranks_deterministically_by_confidence_evidence_and_id(self):
        facts = [
            _fact(3, "preference", confidence=0.8, evidence_count=5),
            _fact(2, "preference", confidence=0.9, evidence_count=1),
            _fact(1, "preference", confidence=0.8, evidence_count=5),
        ]

        selected = select_chat_profile_facts(facts, None, limit=2)

        assert [fact.id for fact in selected] == [1, 3]

    @pytest.mark.parametrize("limit", [0, -1])
    def test_non_positive_limit_returns_empty(self, limit):
        assert select_chat_profile_facts([_fact(1)], [1, 0], limit=limit) == []


class TestSelectAdmittedMemoryIndexes:
    @pytest.mark.parametrize(
        ("existing_count", "expected"),
        [
            (199, [1]),
            (200, []),
            (201, []),
        ],
    )
    def test_capacity_boundaries(self, existing_count, expected):
        importances = [0.7, 0.9, 0.9]

        selected = select_admitted_memory_indexes(
            importances,
            existing_count=existing_count,
            limit=200,
        )

        assert selected == expected
        assert importances == [0.7, 0.9, 0.9]

    def test_returns_original_indexes_by_importance_with_stable_input_order_ties(self):
        selected = select_admitted_memory_indexes(
            [0.6, 0.9, 0.9, 0.8],
            existing_count=197,
            limit=200,
        )

        assert selected == [1, 2, 3]

    def test_available_capacity_larger_than_input_returns_all_ranked_indexes(self):
        selected = select_admitted_memory_indexes(
            [0.4, 0.8],
            existing_count=0,
            limit=200,
        )

        assert selected == [1, 0]


class TestSelectRelevantMemories:
    @pytest.mark.parametrize("query_embedding", [None, []])
    def test_missing_query_embedding_returns_empty(self, query_embedding):
        assert (
            select_relevant_memories(
                query_embedding,
                [_memory(1, embedding=[1, 0])],
                min_importance=0.60,
                min_similarity=0.35,
                dedup_similarity=0.86,
                limit=3,
            )
            == []
        )

    def test_non_positive_limit_returns_empty(self):
        assert (
            select_relevant_memories(
                [1, 0],
                [_memory(1, embedding=[1, 0])],
                min_importance=0.60,
                min_similarity=0.35,
                dedup_similarity=0.86,
                limit=0,
            )
            == []
        )

    def test_importance_and_query_similarity_boundaries_are_inclusive(self):
        memories = [
            _memory(1, importance=0.5999, embedding=[1, 0]),
            _memory(2, importance=0.60, embedding=_unit_vector(0.35)),
            _memory(3, importance=0.60, embedding=_unit_vector(0.3499)),
            _memory(4, importance=0.60, embedding=None),
            _memory(5, importance=0.60, embedding=[0.35, sqrt(1 - 0.35**2), 0]),
        ]

        selected = select_relevant_memories(
            [1, 0],
            memories,
            min_importance=0.60,
            min_similarity=0.35,
            dedup_similarity=0.86,
            limit=10,
        )

        assert [memory.id for memory in selected] == [2]

    def test_ranks_by_080_similarity_plus_020_importance(self):
        memories = [
            _memory(1, importance=0.60, embedding=_unit_vector(0.90)),
            _memory(2, importance=1.00, embedding=_unit_vector(0.85, sign=-1.0)),
        ]

        selected = select_relevant_memories(
            [1, 0],
            memories,
            min_importance=0.60,
            min_similarity=0.35,
            dedup_similarity=0.86,
            limit=3,
        )

        assert 0.80 * 0.85 + 0.20 * 1.00 > 0.80 * 0.90 + 0.20 * 0.60
        assert [memory.id for memory in selected] == [2, 1]

    def test_equal_scores_use_created_at_then_id_descending(self):
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        memories = [
            _memory(1, created_at=base, embedding=[1, 0]),
            _memory(2, created_at=base + timedelta(days=1), embedding=[1, 0]),
            _memory(3, created_at=base + timedelta(hours=1), embedding=[1, 0]),
            _memory(4, created_at=base + timedelta(hours=1), embedding=[1, 0]),
        ]

        selected = select_relevant_memories(
            [1, 0],
            memories,
            min_importance=0.60,
            min_similarity=0.35,
            dedup_similarity=1.01,
            limit=4,
        )

        assert [memory.id for memory in selected] == [2, 4, 3, 1]

    def test_normalized_duplicate_text_keeps_only_the_higher_ranked_memory(self):
        memories = [
            _memory(1, text="shared   event", importance=0.80, embedding=_unit_vector(0.8)),
            _memory(2, text=" shared\nevent ", importance=0.90, embedding=_unit_vector(0.9)),
        ]

        selected = select_relevant_memories(
            [1, 0],
            memories,
            min_importance=0.60,
            min_similarity=0.35,
            dedup_similarity=1.01,
            limit=3,
        )

        assert [memory.id for memory in selected] == [2]

    def test_diversity_includes_086_boundary_and_accepts_just_below(self):
        memories = [
            _memory(1, importance=1.0, embedding=[1, 0]),
            _memory(2, importance=1.0, embedding=_unit_vector(0.86)),
            _memory(3, importance=0.99, embedding=_unit_vector(0.8599)),
        ]

        selected = select_relevant_memories(
            [1, 0],
            memories,
            min_importance=0.60,
            min_similarity=0.35,
            dedup_similarity=0.86,
            limit=3,
        )

        assert [memory.id for memory in selected] == [1, 3]

    def test_returns_only_top_three_diverse_memories(self):
        query = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        memories = []
        for memory_id in range(1, 6):
            embedding = [0.5, 0.0, 0.0, 0.0, 0.0, 0.0]
            embedding[memory_id] = sqrt(0.75)
            memories.append(
                _memory(
                    memory_id,
                    importance=0.5 + 0.1 * memory_id,
                    embedding=embedding,
                )
            )

        selected = select_relevant_memories(
            query,
            memories,
            min_importance=0.60,
            min_similarity=0.35,
            dedup_similarity=0.86,
            limit=3,
        )

        assert [memory.id for memory in selected] == [5, 4, 3]
