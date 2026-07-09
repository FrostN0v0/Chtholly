"""Configuration model and built-in prompt scaffold for llm_chat."""

from dataclasses import field

from arclet.entari import BasicConfModel

from .core.prompts import (
    DEFAULT_PERSONA,
    DEFAULT_IMAGE_TAG_PROMPT,
    DEFAULT_IMAGE_DESCRIBE_PROMPT,
)


class LLMChatConfig(BasicConfModel):
    persona: str = DEFAULT_PERSONA
    """Character text ONLY; framework rules live in SYSTEM_SCAFFOLD."""
    context_window: int = 20
    """Number of history messages loaded per reply."""
    model: str | None = None
    """Model alias for conversation; None uses the llm plugin default."""
    eval_model: str | None = None
    """Model alias for relationship evaluation; None uses the main model."""
    eval_every_n: int = 1
    """Run the relationship evaluator every N bot replies (per user)."""
    eval_context_window: int = 8
    """Recent history lines included as evaluator context."""
    memory_enabled: bool = True
    """Enable long-term profile and semantic memory retrieval."""
    memory_embedding_model: str = "volcengine/doubao-embedding-vision-251215"
    """Embedding model; '-vision-' models use Ark's multimodal endpoint."""
    memory_embedding_api_key: str | None = None
    """Embedding API key; set from env in entari.yml."""
    memory_embedding_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    """Embedding API base URL."""
    memory_top_profile_facts: int = 8
    """Max stable profile facts injected into the prompt."""
    memory_top_memories: int = 5
    """Max semantically relevant memories injected into the prompt."""
    memory_min_similarity: float = 0.25
    """Minimum cosine similarity for relevant episodic memories."""
    memory_dedup_similarity: float = 0.92
    """Cosine threshold treating a new memory as duplicate of a stored one."""
    profile_value_similarity: float = 0.90
    """Cosine threshold treating a patch value as the same fact (reinforce)."""
    profile_fact_min_confidence: float = 0.55
    """Minimum confidence for storing and injecting stable profile facts."""
    memory_max_records_per_user: int = 200
    """Max episodic memory rows kept per user/channel."""
    tts_enabled: bool = True
    """Effective only when the tts_service plugin is installed."""
    tts_max_chars: int = 80
    """speak() truncates text to at most this many chars (sentence boundary)."""
    image_tags_enabled: bool = True
    """Tag local images with vision keywords on startup."""
    image_tag_model: str | None = None
    """Model alias/name for image tagging; None uses the llm plugin default."""
    image_tag_prompt: str = DEFAULT_IMAGE_TAG_PROMPT
    """Vision system prompt for image tag extraction."""
    image_understanding_enabled: bool = True
    """Enable inbound image understanding; vision-capable chat models receive images directly."""
    image_describe_prompt: str = DEFAULT_IMAGE_DESCRIBE_PROMPT
    """Fallback vision prompt used when the chat model cannot receive images directly."""
    image_describe_max_per_message: int = 3
    """Max inbound images attached or described per message; extras degrade to bare placeholders."""
    tag_batch_size: int = 20
    """Max images tagged per startup pass."""
    tag_concurrency: int = 4
    """Concurrent vision/embedding requests per tagging pass."""
    image_match_min_similarity: float = 0.30
    """Minimum cosine similarity for semantic image retrieval."""
    image_top_candidates: int = 8
    """Random pick pool size among top semantic image matches."""
    allowed_commands: list[str] = field(default_factory=list)
    """Command whitelist for the call_plugin tool."""
