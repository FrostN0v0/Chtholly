"""Behavioral contracts for the reply-intent (engagement) policy."""

from __future__ import annotations

import json
from typing import Any, cast
from pathlib import Path

from arclet.entari.config import EntariConfig

if not hasattr(EntariConfig, "instance"):
    setattr(EntariConfig, "instance", EntariConfig.load(Path(__file__).resolve().parents[1] / "entari.yml"))

from plugins.llm_chat.models import AgentEvent
from plugins.llm_chat.core.delivery import DEFAULT_DELIVERY_LIMITS
from plugins.llm_chat.core.engagement import (
    EngagementSignals,
    turn_feedback,
    decide_engagement,
    engagement_budget,
    apply_turn_feedback,
    apply_engagement_budget,
    engagement_event_payload,
    engagement_prompt_context,
)
from plugins.llm_chat.agent_event_view import serialize_event_view


def _signals(**overrides: object) -> EngagementSignals:
    """Build signals for a default stranger, overriding only what a test needs."""

    base: dict[str, object] = {
        "affection": 30.0,
        "trust": 30.0,
        "familiarity": 0.0,
        "irritation": 0.0,
        "user_mood": 0.0,
        "energy": 1.0,
        "text": "在吗",
    }
    base.update(overrides)
    return EngagementSignals(**cast(Any, base))


def test_default_stranger_keeps_full_reply_behaviour() -> None:
    """Calibration guard: the common case must not regress into terse replies.

    Production data showed 116/124 users sitting at the default axes, so a
    stranger landing in ``cold``/``brief`` would silently make the bot colder to
    almost everyone.
    """

    decision = decide_engagement(_signals())
    assert decision.warmth == "neutral"
    assert decision.level == "full"
    assert decision.replies is True

    budget = engagement_budget(decision.level, DEFAULT_DELIVERY_LIMITS)
    assert budget.max_text_messages == DEFAULT_DELIVERY_LIMITS.max_text_messages
    assert budget.allow_topic_extension is True
    assert budget.allow_followup_question is True


def test_warmth_rises_with_relationship_state() -> None:
    assert decide_engagement(_signals(affection=60.0, trust=50.0, familiarity=40.0)).warmth == "close"
    assert decide_engagement(_signals(familiarity=100.0)).warmth == "warm"
    assert decide_engagement(_signals(irritation=60.0)).warmth == "cold"


def test_silence_requires_sustained_irritation_not_one_bad_message() -> None:
    """One hostile message must not trigger silence; repeated ones eventually do."""

    first = decide_engagement(_signals(text="你烦人", irritation=0.0))
    assert first.replies is True
    assert first.level == "reaction_only"

    irritation = 0.0
    levels: list[str] = []
    for _ in range(12):
        signals = _signals(text="你烦人", irritation=irritation, consecutive_user_messages=2)
        decision = decide_engagement(signals)
        levels.append(decision.level)
        feedback = turn_feedback(signals, declined=not decision.replies)
        irritation = apply_turn_feedback(
            irritation=irritation,
            user_mood=0.0,
            familiarity=0.0,
            feedback=feedback,
        )["irritation"]

    assert "declined" in levels, levels
    assert levels[0] != "declined"


def test_irritation_decays_so_silence_is_not_permanent() -> None:
    irritation = 80.0
    for _ in range(40):
        signals = _signals(text="今天天气不错", irritation=irritation)
        decision = decide_engagement(signals)
        feedback = turn_feedback(signals, declined=not decision.replies)
        irritation = apply_turn_feedback(
            irritation=irritation,
            user_mood=0.0,
            familiarity=0.0,
            feedback=feedback,
        )["irritation"]

    assert irritation == 0.0
    assert decide_engagement(_signals(irritation=irritation)).replies is True


def test_obligated_messages_are_always_answered() -> None:
    """Safety boundary: silence must never swallow help, commands, or operators."""

    hostile_and_angry: dict[str, object] = {"irritation": 95.0, "text": "闭嘴"}
    for label, extra in (
        ("command", {"is_command": True}),
        ("operator", {"is_operator": True}),
        ("private", {"is_private": True}),
        ("media", {"requires_media_reply": True}),
    ):
        signals = _signals(**{**hostile_and_angry, **extra})
        decision = decide_engagement(signals)
        assert decision.replies is True, label
        assert decision.obligated is True, label

    asking = decide_engagement(_signals(irritation=95.0, text="帮我看下这个怎么办"))
    assert asking.replies is True
    assert asking.obligated is True


def test_repeated_declines_recover_to_a_minimal_reply() -> None:
    """Consecutive silence must not become an effective block."""

    silent = decide_engagement(_signals(irritation=95.0, text="喂"))
    assert silent.level == "declined"

    recovered = decide_engagement(_signals(irritation=95.0, text="喂", consecutive_declines=2))
    assert recovered.level == "reaction_only"
    assert recovered.replies is True


def test_budget_only_tightens_configured_delivery_limits() -> None:
    full = engagement_budget("full", DEFAULT_DELIVERY_LIMITS)
    unchanged = apply_engagement_budget(DEFAULT_DELIVERY_LIMITS, full)
    assert unchanged == DEFAULT_DELIVERY_LIMITS

    reaction = engagement_budget("reaction_only", DEFAULT_DELIVERY_LIMITS)
    tightened = apply_engagement_budget(DEFAULT_DELIVERY_LIMITS, reaction)
    assert tightened.max_text_messages == 1
    assert tightened.max_text_chars_per_message < DEFAULT_DELIVERY_LIMITS.max_text_chars_per_message
    assert tightened.min_interval_seconds == DEFAULT_DELIVERY_LIMITS.min_interval_seconds

    declined = apply_engagement_budget(DEFAULT_DELIVERY_LIMITS, engagement_budget("declined", DEFAULT_DELIVERY_LIMITS))
    assert declined.max_text_messages == 0
    assert declined.max_media_messages == 0


def test_low_energy_shortens_replies_even_for_close_users() -> None:
    decision = decide_engagement(_signals(affection=60.0, trust=50.0, familiarity=40.0, energy=0.3))
    assert decision.warmth == "close"
    assert decision.level == "brief"
    assert "当前精力偏低" in decision.reasons


def test_prompt_contract_exposes_tone_without_leaking_raw_axes() -> None:
    signals = _signals(affection=60.0, trust=50.0, familiarity=40.0)
    decision = decide_engagement(signals)
    context = engagement_prompt_context(decision, engagement_budget(decision.level, DEFAULT_DELIVERY_LIMITS))

    assert context["level"] == "full"
    assert context["warmth"] == "close"
    assert isinstance(context["tone"], str)
    assert context["tone"]
    serialized = json.dumps(context, ensure_ascii=False)
    for raw_axis in ("60.0", "50.0", "40.0"):
        assert raw_axis not in serialized


def test_engagement_event_is_auditable_and_rendered_for_the_panel() -> None:
    signals = _signals(irritation=95.0, text="闭嘴")
    decision = decide_engagement(signals)
    budget = engagement_budget(decision.level, DEFAULT_DELIVERY_LIMITS)
    payload = engagement_event_payload(decision, budget, signals)

    assert payload["level"] == "declined"
    assert cast(list[str], payload["reasons"])
    assert cast(dict[str, Any], payload["signals"])["irritation"] == 95.0

    event = AgentEvent(
        event_ref="event_engagement",
        turn_id=1,
        sequence=1,
        event_type="engagement_decision",
        role="",
        payload_json=json.dumps(payload, ensure_ascii=False),
        status="declined",
    )
    view = serialize_event_view(event, cast(Any, payload))
    assert view["title"] == "回应意向"
    engagement = cast(dict[str, Any], view["engagement"])
    assert engagement["level_label"] == "不回应"
    assert engagement["warmth_label"] == "冷淡"
    assert engagement["tone"]
    assert engagement["reasons"]

    other = AgentEvent(
        event_ref="event_other",
        turn_id=1,
        sequence=2,
        event_type="assistant_output",
        role="assistant",
        payload_json="{}",
    )
    assert serialize_event_view(other, {})["engagement"] is None
