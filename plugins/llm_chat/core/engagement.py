"""Pure reply-intent policy deciding whether and how warmly to answer.

This module owns the decision that precedes generation. It never performs I/O and
never imports Entari, so the whole policy stays unit-testable and deterministic.
"""

from __future__ import annotations

import re
from typing import Literal
from dataclasses import replace, dataclass
from collections.abc import Mapping, Sequence

from .delivery import DeliveryLimits

EngagementLevel = Literal["full", "brief", "reaction_only", "declined"]

_LEVEL_ORDER: tuple[EngagementLevel, ...] = ("declined", "reaction_only", "brief", "full")
_WARMTH_LABELS = {
    "cold": "语气克制冷淡，只回答必要内容，不主动延伸话题",
    "neutral": "语气平稳自然，不刻意亲近也不冷淡",
    "warm": "语气自然亲近，可以适度延伸话题",
    "close": "语气亲昵主动，愿意延伸话题并主动关心",
}
_OBLIGATION_PATTERNS = (
    re.compile(r"(帮忙|帮我|求助|救命|怎么办|急|请问|麻烦你|拜托)"),
    re.compile(r"(对不起|抱歉|道歉|我错了)"),
    re.compile(r"[?？]"),
)
_HOSTILE_PATTERNS = (
    re.compile(r"(闭嘴|滚|傻逼|煩|烦人|废物|智障|白痴)"),
    re.compile(r"(你他妈|去死)"),
)


@dataclass(frozen=True, slots=True)
class EngagementSignals:
    """Read-only inputs describing one addressed message."""

    affection: float = 30.0
    trust: float = 30.0
    familiarity: float = 0.0
    irritation: float = 0.0
    user_mood: float = 0.0
    energy: float = 1.0
    text: str = ""
    consecutive_user_messages: int = 1
    seconds_since_last_reply: float = 3600.0
    consecutive_declines: int = 0
    is_command: bool = False
    is_private: bool = False
    is_operator: bool = False
    requires_media_reply: bool = False


@dataclass(frozen=True, slots=True)
class EngagementDecision:
    """One reply-intent decision plus the reasons that produced it."""

    level: EngagementLevel
    warmth: str
    reasons: tuple[str, ...]
    obligated: bool

    @property
    def replies(self) -> bool:
        return self.level != "declined"

    @property
    def warmth_hint(self) -> str:
        return _WARMTH_LABELS.get(self.warmth, _WARMTH_LABELS["neutral"])


@dataclass(frozen=True, slots=True)
class EngagementBudget:
    """Behaviour allowances derived from one engagement level."""

    max_text_messages: int
    max_text_chars_per_message: int
    max_media_messages: int
    allow_followup_question: bool
    allow_topic_extension: bool
    allow_stickers: bool


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _matches_any(text: str, patterns: Sequence[re.Pattern[str]]) -> bool:
    return any(pattern.search(text) for pattern in patterns)


def _lower_level(level: EngagementLevel, floor: EngagementLevel) -> EngagementLevel:
    """Return the weaker of two levels using the canonical ordering."""

    return level if _LEVEL_ORDER.index(level) <= _LEVEL_ORDER.index(floor) else floor


def _raise_level(level: EngagementLevel, floor: EngagementLevel) -> EngagementLevel:
    """Return the stronger of two levels using the canonical ordering."""

    return level if _LEVEL_ORDER.index(level) >= _LEVEL_ORDER.index(floor) else floor


def is_obligated(signals: EngagementSignals) -> tuple[bool, str]:
    """Return whether one message must be answered regardless of relationship."""

    if signals.is_command:
        return True, "命令必须响应"
    if signals.is_operator:
        return True, "管理员必须响应"
    if signals.is_private:
        return True, "私聊必须响应"
    if signals.requires_media_reply:
        return True, "图片输入需要文字回应"
    if _matches_any(signals.text, _OBLIGATION_PATTERNS):
        return True, "包含求助、提问或道歉"
    return False, ""


def warmth_band(signals: EngagementSignals) -> str:
    """Map relationship and current feeling onto one warmth band.

    Thresholds are calibrated against the observed production distribution
    (median 21.1, p90 24.9) so a default stranger lands in ``neutral`` rather
    than ``cold``; otherwise almost every user would collapse into one band.
    """

    score = (
        0.45 * _clamp(signals.affection, 0.0, 100.0)
        + 0.25 * _clamp(signals.trust, 0.0, 100.0)
        + 0.20 * _clamp(signals.familiarity, 0.0, 100.0)
        + 12.0 * _clamp(signals.user_mood, -1.0, 1.0)
        - 0.35 * _clamp(signals.irritation, 0.0, 100.0)
    )
    if score >= 45.0:
        return "close"
    if score >= 27.0:
        return "warm"
    if score >= 18.0:
        return "neutral"
    return "cold"


def decide_engagement(signals: EngagementSignals) -> EngagementDecision:
    """Decide whether to reply and how warmly, with auditable reasons."""

    reasons: list[str] = []
    obligated, obligation_reason = is_obligated(signals)
    if obligation_reason:
        reasons.append(obligation_reason)

    warmth = warmth_band(signals)
    # ``neutral`` holds the majority of real users, so it must preserve the
    # existing full-reply behaviour; only ``cold`` starts out restrained.
    warmth_levels: dict[str, EngagementLevel] = {
        "close": "full",
        "warm": "full",
        "neutral": "full",
        "cold": "brief",
    }
    level: EngagementLevel = warmth_levels[warmth]
    reasons.append(f"关系热度为 {warmth}")

    if warmth == "cold" and signals.irritation >= 45.0:
        level = _lower_level(level, "reaction_only")
        reasons.append("积累的不快较高")
    if signals.irritation >= 70.0:
        level = _lower_level(level, "declined")
        reasons.append("不快达到不愿回应的程度")
    if _matches_any(signals.text, _HOSTILE_PATTERNS):
        level = _lower_level(level, "reaction_only")
        reasons.append("对方言辞带有攻击性")
    if signals.consecutive_user_messages >= 4 and warmth == "cold":
        level = _lower_level(level, "reaction_only")
        reasons.append("连续追问显得打扰")
    elif signals.consecutive_user_messages >= 6 and warmth == "neutral":
        level = _lower_level(level, "brief")
        reasons.append("连续追问较多")
    if signals.energy <= 0.35:
        level = _lower_level(level, "brief")
        reasons.append("当前精力偏低")
    if signals.user_mood <= -0.5 and warmth == "cold":
        level = _lower_level(level, "reaction_only")
        reasons.append("对该用户情绪明显低落")

    if signals.consecutive_declines >= 2 and level == "declined":
        level = "reaction_only"
        reasons.append("已连续不回应，避免形同拉黑")
    if obligated:
        level = _raise_level(level, "brief")

    return EngagementDecision(
        level=level,
        warmth=warmth,
        reasons=tuple(reasons),
        obligated=obligated,
    )


def engagement_budget(
    level: EngagementLevel,
    limits: DeliveryLimits,
    *,
    media_requested: bool = False,
) -> EngagementBudget:
    """Keep requested deliverables within configuration, not conversational energy limits."""

    if level == "declined":
        return EngagementBudget(0, 0, 0, False, False, False)
    media_limit = limits.max_media_messages if media_requested else min(1, limits.max_media_messages)
    if level == "reaction_only":
        return EngagementBudget(
            max_text_messages=min(1, limits.max_text_messages),
            max_text_chars_per_message=min(60, limits.max_text_chars_per_message),
            max_media_messages=media_limit,
            allow_followup_question=False,
            allow_topic_extension=False,
            allow_stickers=True,
        )
    if level == "brief":
        return EngagementBudget(
            max_text_messages=min(2, limits.max_text_messages),
            max_text_chars_per_message=min(180, limits.max_text_chars_per_message),
            max_media_messages=media_limit,
            allow_followup_question=False,
            allow_topic_extension=False,
            allow_stickers=True,
        )
    return EngagementBudget(
        max_text_messages=limits.max_text_messages,
        max_text_chars_per_message=limits.max_text_chars_per_message,
        max_media_messages=limits.max_media_messages,
        allow_followup_question=True,
        allow_topic_extension=True,
        allow_stickers=True,
    )


def apply_engagement_budget(limits: DeliveryLimits, budget: EngagementBudget) -> DeliveryLimits:
    """Tighten delivery limits to one engagement budget, never loosening them."""

    return replace(
        limits,
        max_text_messages=min(limits.max_text_messages, budget.max_text_messages),
        max_text_chars_per_message=min(
            limits.max_text_chars_per_message,
            budget.max_text_chars_per_message,
        ),
        max_media_messages=min(limits.max_media_messages, budget.max_media_messages),
    )


def engagement_prompt_context(
    decision: EngagementDecision,
    budget: EngagementBudget,
) -> dict[str, object]:
    """Render the model-visible reply-intent contract for one turn."""

    return {
        "level": decision.level,
        "warmth": decision.warmth,
        "tone": decision.warmth_hint,
        "max_messages": budget.max_text_messages,
        "max_chars_per_message": budget.max_text_chars_per_message,
        "allow_followup_question": budget.allow_followup_question,
        "allow_topic_extension": budget.allow_topic_extension,
    }


def engagement_event_payload(
    decision: EngagementDecision,
    budget: EngagementBudget,
    signals: EngagementSignals,
) -> dict[str, object]:
    """Build the durable, auditable record of one engagement decision."""

    return {
        "level": decision.level,
        "warmth": decision.warmth,
        "obligated": decision.obligated,
        "reasons": list(decision.reasons),
        "budget": {
            "max_text_messages": budget.max_text_messages,
            "max_text_chars_per_message": budget.max_text_chars_per_message,
            "max_media_messages": budget.max_media_messages,
            "allow_followup_question": budget.allow_followup_question,
            "allow_topic_extension": budget.allow_topic_extension,
        },
        "signals": {
            "affection": round(signals.affection, 3),
            "trust": round(signals.trust, 3),
            "familiarity": round(signals.familiarity, 3),
            "irritation": round(signals.irritation, 3),
            "user_mood": round(signals.user_mood, 3),
            "energy": round(signals.energy, 3),
            "consecutive_user_messages": signals.consecutive_user_messages,
            "consecutive_declines": signals.consecutive_declines,
        },
    }


def signals_from_state(
    relation: Mapping[str, object],
    *,
    user_mood: float,
    energy: float,
    text: str,
    consecutive_user_messages: int = 1,
    seconds_since_last_reply: float = 3600.0,
    consecutive_declines: int = 0,
    is_command: bool = False,
    is_private: bool = False,
    is_operator: bool = False,
    requires_media_reply: bool = False,
) -> EngagementSignals:
    """Build signals from stored relationship values without importing ORM types."""

    def _number(key: str, default: float) -> float:
        value = relation.get(key, default)
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default

    return EngagementSignals(
        affection=_number("affection", 30.0),
        trust=_number("trust", 30.0),
        familiarity=_number("familiarity", 0.0),
        irritation=_number("irritation", 0.0),
        user_mood=user_mood,
        energy=energy,
        text=text,
        consecutive_user_messages=max(1, consecutive_user_messages),
        seconds_since_last_reply=max(0.0, seconds_since_last_reply),
        consecutive_declines=max(0, consecutive_declines),
        is_command=is_command,
        is_private=is_private,
        is_operator=is_operator,
        requires_media_reply=requires_media_reply,
    )


@dataclass(frozen=True, slots=True)
class TurnFeedback:
    """Per-turn deterministic adjustment applied to stored relationship state."""

    irritation_delta: float
    mood_delta: float
    closeness_delta: float
    reasons: tuple[str, ...]


def turn_feedback(signals: EngagementSignals, *, declined: bool) -> TurnFeedback:
    """Derive small per-turn deltas so state moves on every turn, not 1-in-N.

    The periodic evaluator still owns calibration and long-term memory; this only
    supplies the continuous drift that keeps irritation and warmth reachable.
    """

    reasons: list[str] = []
    irritation = 0.0
    mood = 0.0
    closeness = 0.4

    if _matches_any(signals.text, _HOSTILE_PATTERNS):
        irritation += 18.0
        mood -= 0.12
        closeness = 0.0
        reasons.append("对方言辞带有攻击性")
    if signals.consecutive_user_messages >= 4:
        irritation += 6.0
        reasons.append("连续追问显得打扰")
    if _matches_any(signals.text, _OBLIGATION_PATTERNS):
        closeness += 0.3
        reasons.append("认真提问或求助")
    if signals.seconds_since_last_reply <= 5.0 and signals.consecutive_user_messages >= 2:
        irritation += 3.0
        reasons.append("催促过于频繁")

    # Irritation is short-lived: it always decays toward zero between turns.
    decay = 4.0 if not declined else 1.0
    irritation -= decay
    if not reasons:
        mood += 0.02

    return TurnFeedback(
        irritation_delta=irritation,
        mood_delta=_clamp(mood, -0.3, 0.3),
        closeness_delta=closeness,
        reasons=tuple(reasons),
    )


def apply_turn_feedback(
    *,
    irritation: float,
    user_mood: float,
    familiarity: float,
    feedback: TurnFeedback,
) -> dict[str, float]:
    """Apply one turn's deltas, keeping every value inside its safe range."""

    return {
        "irritation": _clamp(irritation + feedback.irritation_delta, 0.0, 100.0),
        "user_mood": _clamp(user_mood + feedback.mood_delta, -1.0, 1.0),
        "familiarity": _clamp(familiarity + feedback.closeness_delta, 0.0, 100.0),
    }
