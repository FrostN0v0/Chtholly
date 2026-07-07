"""Unit tests for the pure persona engine: stance table, energy curve, prompt."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from llm_chat_src.persona.compose import (  # noqa: E402
    energy_at,
    mood_desc,
    energy_desc,
    derive_stance,
    compose_persona_prompt,
)


class TestDeriveStance:
    def test_yandere_beats_hostile(self):
        # affection=70, resentment=70: rule 1 (yandere) must win over rule 2 (hostile)
        assert derive_stance(70, 50, 0, 70).startswith("病娇倾向")

    def test_hostile(self):
        assert derive_stance(10, 10, 0, 80).startswith("厌恶")

    def test_tsundere(self):
        assert derive_stance(65, 35, 0, 0).startswith("傲娇")

    def test_clingy(self):
        assert derive_stance(55, 50, 70, 0).startswith("依赖黏人")

    def test_intimate(self):
        assert derive_stance(75, 65, 0, 0).startswith("亲密")

    def test_friendly(self):
        assert derive_stance(50, 50, 0, 0).startswith("友好")

    def test_wary(self):
        assert derive_stance(30, 30, 0, 40).startswith("戒备")

    def test_plain_default(self):
        assert derive_stance(30, 30, 0, 0).startswith("平淡")

    def test_boundary_affection_60_trust_39_is_tsundere(self):
        assert derive_stance(60, 39, 0, 0).startswith("傲娇")

    def test_boundary_affection_60_trust_40_not_tsundere(self):
        assert not derive_stance(60, 40, 0, 0).startswith("傲娇")

    def test_boundary_resentment_60_exact(self):
        assert derive_stance(59, 30, 0, 60).startswith("厌恶")

    def test_boundary_dependence_60_affection_50(self):
        assert derive_stance(50, 50, 60, 0).startswith("依赖黏人")

    def test_boundary_affection_45_friendly(self):
        assert derive_stance(45, 30, 0, 0).startswith("友好")

    def test_boundary_resentment_30_wary(self):
        assert derive_stance(30, 30, 0, 30).startswith("戒备")


class TestEnergyCurve:
    def test_bands(self):
        assert energy_at(0) == 0.3
        assert energy_at(6) == 0.3
        assert energy_at(7) == 0.8
        assert energy_at(11) == 0.8
        assert energy_at(12) == 1.0
        assert energy_at(17) == 1.0
        assert energy_at(18) == 0.7
        assert energy_at(22) == 0.7
        assert energy_at(23) == 0.4


class TestDescriptors:
    def test_mood_bands(self):
        assert mood_desc(0.6) == "开朗雀跃"
        assert mood_desc(0.3) == "心情不错"
        assert mood_desc(0.0) == "平静"
        assert mood_desc(-0.3) == "有点低落"
        assert mood_desc(-0.7) == "烦躁易怒"

    def test_mood_boundaries(self):
        assert mood_desc(0.5) == "开朗雀跃"
        assert mood_desc(0.1) == "心情不错"
        assert mood_desc(-0.1) == "有点低落"
        assert mood_desc(-0.5) == "烦躁易怒"

    def test_energy_bands(self):
        assert energy_desc(0.8) == "精力充沛"
        assert energy_desc(0.5) == "状态正常"
        assert energy_desc(0.4).startswith("有点困倦")


class TestComposePrompt:
    def test_structure_and_no_scaffold_leak_into_persona(self):
        prompt = compose_persona_prompt(
            "你是珂朵莉。",
            0.0,
            1.0,
            affection=80,
            trust=20,
            dependence=0,
            resentment=0,
            familiarity=10,
            impression="",
            user_name="测试员",
        )
        assert prompt.startswith("你是珂朵莉。")
        assert "[当前状态]" in prompt
        assert "[对话对象:测试员]" in prompt
        assert "傲娇" in prompt
        assert "还不了解这个人" in prompt

    def test_impression_included(self):
        prompt = compose_persona_prompt(
            "persona",
            0.0,
            1.0,
            affection=30,
            trust=30,
            dependence=0,
            resentment=0,
            familiarity=70,
            impression="爱开玩笑的老朋友",
            user_name="A",
        )
        assert "爱开玩笑的老朋友" in prompt
        assert "老朋友" in prompt  # familiarity hint present at >= 60
