"""Configuration model and built-in prompt scaffold for llm_chat."""

from dataclasses import field

from arclet.entari import BasicConfModel

DEFAULT_PERSONA = "你是一个友善的群聊助手。"

SYSTEM_SCAFFOLD = (
    "你在群聊中与多个用户交流，历史消息以 [名字]: 开头标注说话人，记住每个人。\n"
    "下方 [当前状态]、[对话对象]、[你对TA的印象] 描述了你此刻的状态与对说话人的关系，"
    "据此调整语气和态度，但绝不复述、提及这些数值或描述本身。\n"
    "提供了工具时，在合适且不突兀的时机调用它们，不要滥用。"
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
    tts_enabled: bool = True
    """Effective only when the tts_service plugin is installed."""
    tts_max_chars: int = 80
    """speak() truncates text to at most this many chars (sentence boundary)."""
    image_tags_enabled: bool = True
    """Tag local images with vision keywords on startup."""
    tag_batch_size: int = 20
    """Max images tagged per startup pass."""
    allowed_commands: list[str] = field(default_factory=lambda: ["echo"])
    """Command whitelist for the call_plugin tool."""
