"""Pure persona composition: stance derivation + system prompt assembly.

Archetypes (tsundere / yandere / clingy / hostile ...) EMERGE from axis
combinations via a first-match rule table; no single scalar decides tone.
"""

from ..config import SYSTEM_SCAFFOLD

# (predicate, stance descriptor) — first match wins; order is load-bearing.
_STANCE_RULES: list[tuple] = [
    (
        lambda a, t, d, r: r >= 60 and a >= 60,
        "病娇倾向：强烈的占有欲与嫉妒，爱恨交织，言语偏执而黏人",
    ),
    (
        lambda a, t, d, r: r >= 60,
        "厌恶：冷淡带刺，不耐烦，想尽快结束对话",
    ),
    (
        lambda a, t, d, r: a >= 60 and t < 40,
        "傲娇：明明在意却嘴硬否认，口是心非，别扭",
    ),
    (
        lambda a, t, d, r: d >= 60 and a >= 50,
        "依赖黏人：渴望被关注，害怕被冷落，撒娇求陪伴",
    ),
    (
        lambda a, t, d, r: a >= 70 and t >= 60,
        "亲密：温柔坦率，主动关心，偶尔害羞",
    ),
    (
        lambda a, t, d, r: a >= 45,
        "友好：自然友善，愿意闲聊",
    ),
    (
        lambda a, t, d, r: r >= 30,
        "戒备：礼貌但保持距离，话中略有不满",
    ),
]

_FALLBACK_STANCE = "平淡：普通认识，客气但不热络"


def derive_stance(affection: float, trust: float, dependence: float, resentment: float) -> str:
    for predicate, descriptor in _STANCE_RULES:
        if predicate(affection, trust, dependence, resentment):
            return descriptor
    return _FALLBACK_STANCE


def mood_desc(mood: float) -> str:
    if mood >= 0.5:
        return "开朗雀跃"
    if mood >= 0.1:
        return "心情不错"
    if mood > -0.1:
        return "平静"
    if mood > -0.5:
        return "有点低落"
    return "烦躁易怒"


def energy_desc(energy: float) -> str:
    if energy >= 0.8:
        return "精力充沛"
    if energy >= 0.5:
        return "状态正常"
    return "有点困倦、回复慵懒简短"


def energy_at(hour: int) -> float:
    """Energy as a pure function of local hour (not stored)."""
    if 0 <= hour <= 6:
        return 0.3
    if 7 <= hour <= 11:
        return 0.8
    if 12 <= hour <= 17:
        return 1.0
    if 18 <= hour <= 22:
        return 0.7
    return 0.4  # hour == 23


def familiarity_hint(familiarity: float) -> str:
    if familiarity >= 60:
        return "，是老朋友了，可以提旧梗开玩笑"
    if familiarity < 20:
        return "，还是初识，语气收敛些"
    return ""


def compose_persona_prompt(
    persona: str,
    mood: float,
    energy: float,
    *,
    affection: float,
    trust: float,
    dependence: float,
    resentment: float,
    familiarity: float,
    impression: str,
    profile_facts: list[str] | None = None,
    relevant_memories: list[str] | None = None,
    user_name: str,
) -> str:
    """persona + built-in scaffold + state block -> full system prompt."""
    stance = derive_stance(affection, trust, dependence, resentment)
    state_lines = [
        f"[当前状态] 心情:{mood_desc(mood)}，精力:{energy_desc(energy)}",
        (
            f"[对话对象:{user_name}] 关系:{stance}"
            f"（好感{affection:.0f} 信任{trust:.0f} 依赖{dependence:.0f}"
            f" 芥蒂{resentment:.0f} 熟悉{familiarity:.0f}{familiarity_hint(familiarity)}）"
        ),
    ]
    if profile_facts:
        state_lines.append("[长期画像]\n" + "\n".join(profile_facts))
    if relevant_memories:
        state_lines.append("[相关记忆]\n" + "\n".join(relevant_memories))
    state_lines.append(f"[你对TA的最近印象] {impression or '还不了解这个人'}")
    state_block = "\n".join(state_lines)
    return f"{persona}\n\n{SYSTEM_SCAFFOLD}\n\n{state_block}"
