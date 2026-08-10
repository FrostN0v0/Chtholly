"""Small protocols shared by persona runtime modules."""

from __future__ import annotations

from typing import Protocol


class LLMChatConfigLike(Protocol):
    memory_enabled: bool
    memory_embedding_model: str
    memory_embedding_api_key: str | None
    memory_embedding_base_url: str
    memory_top_profile_facts: int
    memory_top_memories: int
    memory_min_importance: float
    memory_min_similarity: float
    memory_dedup_similarity: float
    memory_prompt_dedup_similarity: float
    profile_alias_similarity: float
    memory_eval_profile_fact_limit: int
    profile_value_similarity: float
    profile_fact_min_confidence: float
    memory_max_records_per_user: int
