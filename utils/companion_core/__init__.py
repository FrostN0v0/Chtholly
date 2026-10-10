"""Import-safe contracts, scheduling policy and prompts for companion mode."""

from .models import (
    MAX_LIFE_STATE_CHARS,
    LifePlan,
    LifeEvent,
    LifeState,
    AttentionUpdate,
    ContactProposal,
    ConversationMode,
    ReflectionProposal,
    ReflectionValidationError,
    parse_reflection,
)
from .policy import (
    USER_QUIET_GRACE_MINUTES,
    CompanionLimits,
    CompanionPolicyError,
    contact_ready,
    next_review_at,
    normalize_limits,
)
from .prompts import build_reflection_prompt, build_reflection_system

__all__ = [
    "AttentionUpdate",
    "CompanionLimits",
    "CompanionPolicyError",
    "ContactProposal",
    "ConversationMode",
    "LifeEvent",
    "LifePlan",
    "LifeState",
    "MAX_LIFE_STATE_CHARS",
    "ReflectionProposal",
    "ReflectionValidationError",
    "USER_QUIET_GRACE_MINUTES",
    "build_reflection_prompt",
    "build_reflection_system",
    "contact_ready",
    "next_review_at",
    "normalize_limits",
    "parse_reflection",
]
