"""Configuration model and built-in prompt scaffold for llm_chat."""

from dataclasses import field

from arclet.entari import BasicConfModel

DEFAULT_PERSONA = "你是一个友善的群聊助手。"

SYSTEM_SCAFFOLD = (
    "你在群聊中与多个用户交流，历史消息以 [名字]: 开头标注说话人，记住每个人。\n"
    "下方 [当前状态]、[对话对象]、[长期画像]、[相关记忆]、[你对TA的最近印象] 描述了你此刻的状态与说话人的关系，"
    "据此调整语气和态度，但绝不复述、提及这些数值或描述本身。"
    "长期画像是多轮互动积累的稳定事实；最近印象只描述短期互动。不要因为一次新话题、玩笑或临时情绪推翻长期画像。\n"
    "提供了本地媒体工具时，只在合适且不突兀的时机调用，不要滥用。"
    "send_image 用于图片、表情包、贴纸请求，或轻量情绪反应比文字更合适时；"
    "send_audio 用于语音、台词请求，或短本地语音片段贴合当前情绪/场景时。"
    "调用这些工具时传入紧凑的场景/情绪关键词，例如 害羞 可爱 早安，不要传长篇正文。"
    "不要向用户提及内部标签库、数据库或工具名。"
)

DEFAULT_IMAGE_TAG_PROMPT = (
    "只输出一行中文标签，用中文逗号分隔；不要编号、解释或 Markdown。"
    "给出 12-20 个短标签，优先描述适合聊天选图的情绪、语气、回复场景、主体、动作表情和明显风格。"
    "仅在画面支持时使用如 开心、害羞、生气、吐槽、安慰、早安、晚安、可爱、兽耳少女 等标签。"
    "避免只给 动漫 这类宽泛标签；宽泛风格必须搭配具体情绪或场景。"
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
    tag_batch_size: int = 20
    """Max images tagged per startup pass."""
    image_match_min_similarity: float = 0.30
    """Minimum cosine similarity for semantic image retrieval."""
    image_top_candidates: int = 8
    """Random pick pool size among top semantic image matches."""
    allowed_commands: list[str] = field(default_factory=lambda: ["echo"])
    """Command whitelist for the call_plugin tool."""
