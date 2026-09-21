"""Human-readable projections of durable AgentEvent rows for the WebUI."""

from __future__ import annotations

import json
import math
from typing import cast
from urllib.parse import quote
from collections.abc import Mapping, Sequence

from .models import AgentEvent
from .core.types import JSONType
from .agent_events import load_event_payload
from .agent_attachments import MAX_INPUT_AUDIT_ATTACHMENTS, is_agent_attachment, event_attachment_metadata
from .core.tool_trace_safety import project_message_arguments

_INLINE_OBJECT_CHARS = 8000
_PREVIEW_CHARS = 400
_DETAIL_VALUE_CHARS = 200
_MAX_DETAILS = 8
_MAX_EVIDENCE_IMAGES = 12
_MEME_FILE_ENDPOINT = "/api/llm-chat/memes/files"
_INPUT_ATTACHMENT_ENDPOINT = "/api/llm-chat/sessions/events"

EVENT_TITLES = {
    "user_input": "用户输入",
    "engagement_decision": "回应意向",
    "relationship_evaluation": "\u5173\u7cfb\u4e0e\u60c5\u7eea\u8bc4\u4f30",
    "response_decision": "\u81ea\u4e3b\u56de\u5e94\u7ed3\u679c",
    "model_attempt": "生成尝试（汇总）",
    "model_request": "模型请求",
    "model_response": "模型响应",
    "context_snapshot": "实际上下文快照",
    "assistant_tool_call": "工具调用",
    "tool_result": "工具结果",
    "assistant_output": "旧版回复摘要（送达记录不完整）",
    "message_delivery": "确认送达消息",
    "turn_timing": "用户输入接收时间",
    "context_selection": "上下文选择",
    "persona_state": "人格与记忆",
}
ENGAGEMENT_LEVEL_LABELS = {
    "full": "完整回应",
    "brief": "简短回应",
    "reaction_only": "仅轻回应",
    "declined": "不回应",
}
ENGAGEMENT_WARMTH_LABELS = {
    "cold": "冷淡",
    "neutral": "平稳",
    "warm": "亲近",
    "close": "亲昵",
}
ENGAGEMENT_TONES = {
    "cold": "语气克制冷淡，只回答必要内容",
    "neutral": "语气平稳自然",
    "warm": "语气自然亲近，可适度延伸话题",
    "close": "语气亲昵主动，愿意主动关心",
}
_FIELD_LABELS = {
    "affection": "好感",
    "attempt": "尝试",
    "avoid_when": "避免场景",
    "budgets": "预算",
    "category": "类别",
    "chars": "字符数",
    "command": "命令",
    "confidence": "置信度",
    "content": "内容",
    "context": "检索语境",
    "dedup_similarity": "去重相似度",
    "delay_seconds": "间隔秒数",
    "dependence": "依赖",
    "enabled": "已启用",
    "energy": "精力",
    "error": "错误",
    "error_code": "错误码",
    "estimated_tokens": "估算 Token",
    "eval_counter": "评估计数",
    "evidence_count": "证据条数",
    "excerpt": "摘要",
    "familiarity": "熟悉度",
    "focus": "关注点",
    "fresh_context": "忽略前文",
    "full_session_tokens": "会话 Token",
    "image_paths": "图片路径",
    "images": "图片",
    "importance": "重要度",
    "impression": "印象",
    "key": "字段",
    "limit": "数量上限",
    "markdown": "Markdown",
    "max_input_tokens": "输入上限",
    "meaning": "含义",
    "memories": "命中记忆",
    "memory": "记忆检索",
    "messages": "消息",
    "min_importance": "最低重要度",
    "min_similarity": "最低相似度",
    "model": "模型",
    "mood": "心情",
    "output_reserve_tokens": "输出预留",
    "path": "路径",
    "pending_eval": "本轮将评估",
    "profile_fact_min_confidence": "画像最低置信度",
    "profile_facts": "画像事实",
    "prompt": "提示词",
    "prompt_memories": "注入记忆",
    "prompt_profile": "注入画像",
    "query": "查询",
    "query_embedded": "查询已向量化",
    "relation": "关系轴",
    "resentment": "怨念",
    "result_count": "结果数",
    "returned_count": "返回条数",
    "selection_mode": "选择方式",
    "similarity": "相似度",
    "size": "尺寸",
    "sources": "来源",
    "speaker": "发言人",
    "state": "当前状态",
    "stored_memories": "库存记忆数",
    "stored_profile_facts": "库存画像数",
    "summary": "结果摘要",
    "text": "文本",
    "text_chars": "文本字符数",
    "thresholds": "阈值",
    "timezone": "时区",
    "top_memories": "记忆取数",
    "top_profile_facts": "画像取数",
    "trust": "信任",
    "url": "链接",
    "value": "内容",
    "width": "宽度",
}
_PREVIEW_KEYS = ("content", "text", "summary", "query", "prompt", "excerpt", "command", "context", "markdown")
_SKIPPED_DETAIL_KEYS = frozenset(
    {"content", "context_arguments", "context_result", "metrics", "evidence", "attachments"}
)


def event_title(event: AgentEvent) -> str:
    """Return the operator-facing label for one event."""

    if event.tool_name:
        return event.tool_name
    return EVENT_TITLES.get(event.event_type, event.event_type)


def field_label(key: str) -> str:
    return _FIELD_LABELS.get(key, key)


def _compact(value: str, limit: int) -> str:
    normalized = " ".join(value.split())
    return normalized if len(normalized) <= limit else f"{normalized[: limit - 1]}…"


def _scalar_text(value: object, limit: int = _DETAIL_VALUE_CHARS) -> str:
    if isinstance(value, str):
        return _compact(value, limit)
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def _collection_text(value: object, limit: int = _DETAIL_VALUE_CHARS) -> str:
    if isinstance(value, Mapping):
        parts = [f"{field_label(str(key))}={_scalar_text(item, 60)}" for key, item in value.items()]
        return _compact("，".join(part for part in parts if not part.endswith("=")), limit)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        rendered = [_scalar_text(item, 80) or _collection_text(item, 80) for item in value[:6]]
        joined = "，".join(part for part in rendered if part)
        suffix = f" 等 {len(value)} 项" if len(value) > 6 else ""
        return _compact(f"{joined}{suffix}", limit)
    return _scalar_text(value, limit)


def _payload_section(payload: Mapping[str, JSONType], key: str) -> JSONType | None:
    audit = payload.get(f"audit_{key}")
    if isinstance(audit, Mapping) and "data" in audit:
        return _payload_section(audit, "data")
    if key not in payload:
        return None
    value = payload[key]
    if value is None:
        return None
    serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(serialized) > _INLINE_OBJECT_CHARS:
        return {"stored": True, "chars": len(serialized)}
    return value


def _preview_source(event: AgentEvent, payload: Mapping[str, JSONType]) -> JSONType | Mapping[str, JSONType]:
    key = (
        "arguments"
        if event.event_type == "assistant_tool_call"
        else "result"
        if event.event_type == "tool_result"
        else ""
    )
    if key:
        audit = payload.get(f"audit_{key}")
        if isinstance(audit, Mapping) and "data" in audit:
            return audit["data"]
        return payload.get(key, payload)
    return payload


_MESSAGE_CONTENT_CHARS = 2400
_MESSAGE_QUOTE_CHARS = 500
_MESSAGE_MAX_QUOTES = 8
_MESSAGE_MAX_MENTIONS = 10
_MESSAGE_MAX_LIST_ITEMS = 12


def _clip_message_text(value: str, limit: int, state: list[bool]) -> str:
    if len(value) <= limit:
        return value
    state[0] = True
    return f"{value[: max(0, limit - 1)]}…"


def _display_text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _decode_message_mapping(value: object) -> Mapping[str, object] | None:
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value)
    if not isinstance(value, str) or not value.lstrip().startswith("{"):
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, UnicodeError):
        return None
    return cast(Mapping[str, object], parsed) if isinstance(parsed, Mapping) else None


def _human_message_value(value: object, state: list[bool], *, depth: int = 0) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if depth >= 3:
        state[0] = True
        return "[\u5d4c\u5957\u5185\u5bb9\u5df2\u7701\u7565]"
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        items = list(value)
        rendered: list[str] = []
        for item in items[:_MESSAGE_MAX_LIST_ITEMS]:
            text = _human_message_value(item, state, depth=depth + 1)
            if text:
                rendered.append(text)
        if len(items) > _MESSAGE_MAX_LIST_ITEMS:
            state[0] = True
            rendered.append("…")
        return "\n".join(rendered)
    if isinstance(value, Mapping):
        nested = value.get("content")
        if nested is not None:
            return _human_message_value(nested, state, depth=depth + 1)
        text = value.get("text")
        if isinstance(text, str):
            return text
        data = value.get("data")
        if isinstance(data, Mapping) and isinstance(data.get("text"), str):
            return cast(str, data["text"])
    return ""


def _message_quote(value: object, state: list[bool]) -> dict[str, object] | None:
    mapping = _decode_message_mapping(value)
    if mapping is None:
        content = _human_message_value(value, state)
        return (
            {"speaker": None, "role": "unknown", "content": _clip_message_text(content, _MESSAGE_QUOTE_CHARS, state)}
            if content
            else None
        )
    speaker = _display_text(mapping.get("speaker"))
    role = _display_text(mapping.get("speaker_role")) or _display_text(mapping.get("role"))
    raw_content = mapping.get("content")
    nested = _decode_message_mapping(raw_content)
    if nested is not None and "content" in nested:
        mapping = nested
        raw_content = mapping.get("content")
    content = _human_message_value(raw_content, state)
    if not content:
        return None
    speaker = speaker or _display_text(mapping.get("speaker"))
    role = role or _display_text(mapping.get("speaker_role")) or _display_text(mapping.get("role")) or "unknown"
    return {
        "speaker": speaker,
        "role": role,
        "content": _clip_message_text(content, _MESSAGE_QUOTE_CHARS, state),
    }


def _message_projection(value: object) -> dict[str, object] | None:
    """Project serialized user input without exposing platform metadata."""
    state = [False]
    mapping = _decode_message_mapping(value)
    if mapping is None or "content" not in mapping:
        content = _human_message_value(value, state)
        if not content and isinstance(value, str):
            content = value
        if not content:
            return None
        return {
            "speaker": None,
            "content": _clip_message_text(content, _MESSAGE_CONTENT_CHARS, state),
            "quotes": [],
            "mentions": [],
            "truncated": state[0],
        }

    outer = mapping
    inner = _decode_message_mapping(outer.get("content"))
    if inner is not None and "content" in inner:
        mapping = inner
    raw_content = mapping.get("content")
    content = _clip_message_text(_human_message_value(raw_content, state), _MESSAGE_CONTENT_CHARS, state)
    speaker = _display_text(outer.get("speaker")) or _display_text(mapping.get("speaker"))

    raw_quotes: list[object] = []
    for source in (outer,) if outer is mapping else (outer, mapping):
        candidate = source.get("forwarded_messages")
        if isinstance(candidate, Sequence) and not isinstance(candidate, (str, bytes, bytearray)):
            raw_quotes.extend(candidate)
    quotes: list[dict[str, object]] = []
    for raw_quote in raw_quotes:
        if len(quotes) >= _MESSAGE_MAX_QUOTES:
            state[0] = True
            break
        quote = _message_quote(raw_quote, state)
        if quote is None:
            continue
        quotes.append(quote)

    raw_mentions: list[object] = []
    for source in (outer,) if outer is mapping else (outer, mapping):
        candidate = source.get("mentioned_participants")
        if isinstance(candidate, Sequence) and not isinstance(candidate, (str, bytes, bytearray)):
            raw_mentions.extend(candidate)
    mentions: list[dict[str, str]] = []
    seen_mentions: set[str] = set()
    for raw_mention in raw_mentions:
        if len(mentions) >= _MESSAGE_MAX_MENTIONS:
            state[0] = True
            break
        name = (
            _display_text(raw_mention.get("display_name") or raw_mention.get("name"))
            if isinstance(raw_mention, Mapping)
            else _display_text(raw_mention)
        )
        if not name:
            continue
        name = _clip_message_text(name, 120, state)
        if name in seen_mentions:
            continue
        seen_mentions.add(name)
        mentions.append({"name": name})
    return {
        "speaker": speaker,
        "content": content,
        "quotes": quotes,
        "mentions": mentions,
        "truncated": state[0],
    }


def _unwrap_user_turn(value: str) -> str:
    """Return readable text and attributed quoted context from one user turn."""
    projection = _message_projection(value)
    if projection is None:
        return value
    content = cast(str, projection["content"])
    speaker = projection.get("speaker")
    text = f"{speaker}\uff1a{content}" if isinstance(speaker, str) and speaker else content
    quotes = projection.get("quotes")
    if isinstance(quotes, list):
        for quote in quotes:
            if not isinstance(quote, Mapping):
                continue
            quote_content = quote.get("content")
            if not isinstance(quote_content, str) or not quote_content:
                continue
            quote_speaker = quote.get("speaker")
            label = quote_speaker if isinstance(quote_speaker, str) and quote_speaker else "\u672a\u77e5\u6765\u6e90"
            text += f"\n[\u5f15\u7528 {label}] {quote_content}"
    mentions = projection.get("mentions")
    if isinstance(mentions, list):
        names = [
            item.get("name") for item in mentions if isinstance(item, Mapping) and isinstance(item.get("name"), str)
        ]
        if names:
            text += "\n" + " ".join(f"@{name}" for name in names)
    return text


def _message_preview(arguments: Mapping[str, object]) -> str:
    projection = project_message_arguments(arguments, max_text=_PREVIEW_CHARS)
    segments = projection["segments"]
    if not isinstance(segments, list):
        return ""
    parts: list[str] = []
    for segment in segments:
        if not isinstance(segment, Mapping):
            continue
        kind = segment.get("type")
        if kind in {"text", "link", "style"}:
            parts.append(str(segment.get("text") or segment.get("url") or ""))
        elif kind == "break":
            parts.append("\n")
        elif kind == "mention":
            parts.append("@participant")
        elif kind in {"media", "emoji"}:
            parts.append(f"[{kind}]")
    return _compact("".join(parts), _PREVIEW_CHARS)


def event_preview(event: AgentEvent, payload: Mapping[str, JSONType]) -> str:
    """Return the most informative plain-text content of one event."""

    source = _preview_source(event, payload)
    if event.tool_name == "send_msg" and isinstance(source, Mapping):
        preview = _message_preview(source)
        if preview:
            return preview
    if isinstance(source, str):
        return _compact(source, _PREVIEW_CHARS)
    for key in _PREVIEW_KEYS:
        candidate = source.get(key) if isinstance(source, Mapping) else None
        if isinstance(candidate, str) and candidate.strip():
            text = _unwrap_user_turn(candidate) if event.event_type == "user_input" else candidate
            return _compact(text, _PREVIEW_CHARS)
    result = payload.get("result")
    if isinstance(result, str) and result.strip():
        return _compact(result, _PREVIEW_CHARS)
    if isinstance(source, Mapping):
        return _compact(_collection_text(source, _PREVIEW_CHARS), _PREVIEW_CHARS) or _compact(
            json.dumps(source, ensure_ascii=False, separators=(",", ":")),
            _PREVIEW_CHARS,
        )
    return _compact(json.dumps(source, ensure_ascii=False, separators=(",", ":")), _PREVIEW_CHARS)


def event_details(event: AgentEvent, payload: Mapping[str, JSONType]) -> list[dict[str, str]]:
    """Return stringified key fields worth showing without opening raw JSON."""

    details: list[dict[str, str]] = []
    source = _preview_source(event, payload)
    entries = source if isinstance(source, Mapping) else {}
    for key, value in entries.items():
        if key in _SKIPPED_DETAIL_KEYS or len(details) >= _MAX_DETAILS:
            continue
        rendered = _scalar_text(value) or _collection_text(value)
        if not rendered:
            continue
        details.append({"label": field_label(str(key)), "value": rendered})
    if event.duration_ms:
        details.append({"label": "耗时", "value": f"{event.duration_ms} ms"})
    return details


def _image_url(path: str) -> str:
    normalized = path.replace("\\", "/").strip("/")
    prefix, separator, file_name = normalized.partition("/")
    if prefix != "memes" or not separator or "/" in file_name:
        return ""
    return f"{_MEME_FILE_ENDPOINT}/{quote(file_name, safe='')}"


def event_evidence(payload: Mapping[str, JSONType]) -> dict[str, JSONType] | None:
    """Return which concrete artifacts a tool actually delivered."""

    evidence = payload.get("evidence")
    if not isinstance(evidence, Mapping) or not evidence:
        return None
    normalized: dict[str, JSONType] = {}
    images = evidence.get("images")
    if isinstance(images, Sequence) and not isinstance(images, (str, bytes)):
        rendered: list[JSONType] = []
        for image in images[:_MAX_EVIDENCE_IMAGES]:
            if not isinstance(image, Mapping):
                continue
            path = _scalar_text(image.get("path"), 300)
            rendered.append(
                {
                    "path": path,
                    "url": _image_url(path),
                    "meaning": _scalar_text(image.get("meaning")),
                    "text": _scalar_text(image.get("text")),
                }
            )
        if rendered:
            normalized["images"] = rendered
    for key, value in evidence.items():
        if key == "images" or len(normalized) >= _MAX_DETAILS:
            continue
        rendered_value = _scalar_text(value) or _collection_text(value)
        if rendered_value:
            normalized[str(key)] = rendered_value
    return normalized or None


def _rounded(value: object) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        return f"{value:g}"
    return _scalar_text(value)


def _labelled_rows(value: object) -> list[dict[str, str]]:
    if not isinstance(value, Mapping):
        return []
    rows: list[dict[str, str]] = []
    for key, item in value.items():
        rendered = _rounded(item) or _collection_text(item)
        if rendered:
            rows.append({"label": field_label(str(key)), "value": rendered})
    return rows


def _scored_items(value: object, *, text_key: str) -> list[dict[str, str]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    items: list[dict[str, str]] = []
    for entry in value[:_MAX_EVIDENCE_IMAGES]:
        if not isinstance(entry, Mapping):
            continue
        text = _scalar_text(entry.get(text_key), 300)
        if not text:
            continue
        scores = [
            f"{field_label(name)} {_rounded(entry[name])}"
            for name in ("similarity", "confidence", "importance", "evidence_count")
            if name in entry and _rounded(entry[name])
        ]
        label = _scalar_text(entry.get("category")) or _scalar_text(entry.get("key"))
        items.append({"label": label, "text": text, "scores": "，".join(scores)})
    return items


def event_persona(event: AgentEvent, payload: Mapping[str, JSONType]) -> dict[str, JSONType] | None:
    """Project the persona, relationship, and memory inputs that shaped one turn."""

    if event.event_type != "persona_state" or not payload:
        return None
    memory = payload.get("memory")
    memory_map = memory if isinstance(memory, Mapping) else {}
    profile = payload.get("prompt_profile")
    raw_memories = payload.get("prompt_memories")
    injected_memories = (
        raw_memories if isinstance(raw_memories, Sequence) and not isinstance(raw_memories, (str, bytes)) else ()
    )
    injected_profile = [
        {"label": field_label(str(category)), "text": "，".join(str(item) for item in values)}
        for category, values in (profile.items() if isinstance(profile, Mapping) else ())
        if isinstance(values, Sequence) and not isinstance(values, (str, bytes)) and values
    ]
    return {
        "relation": cast(JSONType, _labelled_rows(payload.get("relation"))),
        "relationship": relationship_snapshot(payload.get("relationship")),
        "state": cast(JSONType, _labelled_rows(payload.get("state"))),
        "budgets": cast(JSONType, _labelled_rows(payload.get("budgets"))),
        "thresholds": cast(JSONType, _labelled_rows(memory_map.get("thresholds"))),
        "retrieval": cast(
            JSONType,
            _labelled_rows(
                {
                    key: memory_map[key]
                    for key in ("enabled", "query_embedded", "stored_profile_facts", "stored_memories")
                    if key in memory_map
                }
            ),
        ),
        "profile_facts": cast(JSONType, _scored_items(memory_map.get("profile_facts"), text_key="value")),
        "memories": cast(JSONType, _scored_items(memory_map.get("memories"), text_key="text")),
        "injected_profile": cast(JSONType, injected_profile),
        "injected_memories": cast(
            JSONType,
            [text for item in injected_memories if (text := _scalar_text(item, 300))],
        ),
    }


def event_engagement(event: AgentEvent, payload: Mapping[str, JSONType]) -> dict[str, JSONType] | None:
    """Project one reply-intent decision for the WebUI."""

    if event.event_type != "engagement_decision" or not payload:
        return None
    level = _scalar_text(payload.get("level"))
    warmth = _scalar_text(payload.get("warmth"))
    budget = payload.get("budget")
    signals = payload.get("signals")
    reasons = payload.get("reasons")
    return {
        "level": level,
        "level_label": ENGAGEMENT_LEVEL_LABELS.get(level, level),
        "warmth": warmth,
        "warmth_label": ENGAGEMENT_WARMTH_LABELS.get(warmth, warmth),
        "tone": ENGAGEMENT_TONES.get(warmth, ""),
        "obligated": payload.get("obligated") is True,
        "reasons": cast(
            JSONType,
            [
                text
                for item in (reasons if isinstance(reasons, Sequence) and not isinstance(reasons, (str, bytes)) else ())
                if (text := _scalar_text(item, 200))
            ],
        ),
        "budget": cast(JSONType, dict(budget) if isinstance(budget, Mapping) else {}),
        "signals": cast(JSONType, dict(signals) if isinstance(signals, Mapping) else {}),
    }


_RELATIONSHIP_AXES = ("affection", "trust", "dependence", "resentment", "familiarity")


def _finite_number(value: object) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _turn_ids(value: object) -> list[int]:
    if not isinstance(value, list):
        return []
    return list(
        dict.fromkeys(item for item in value if isinstance(item, int) and not isinstance(item, bool) and item > 0)
    )


def relationship_snapshot(value: object) -> dict[str, JSONType] | None:
    """Read only the captured snapshot; missing axes and emotions remain unknown."""
    if not isinstance(value, Mapping):
        return None
    axes = value.get("axes")
    raw_emotions = value.get("emotions")
    emotions: list[JSONType] | None = None
    if isinstance(raw_emotions, list) and all(isinstance(item, Mapping) for item in raw_emotions):
        emotions = [
            {
                "name": item.get("name") if isinstance(item.get("name"), str) else None,
                "intensity": _finite_number(item.get("intensity")),
                "cause": item.get("cause") if isinstance(item.get("cause"), str) else None,
                "evidence_turn_ids": cast(JSONType, _turn_ids(item.get("evidence_turn_ids"))),
                "updated_at": _finite_number(item.get("updated_at")),
            }
            for item in raw_emotions
            if isinstance(item, Mapping)
        ]
    return {
        "version": _finite_number(value.get("version")),
        "axes": {key: _finite_number(axes.get(key)) for key in _RELATIONSHIP_AXES}
        if isinstance(axes, Mapping)
        else None,
        "description": value.get("description") if isinstance(value.get("description"), str) else None,
        "impression": value.get("impression") if isinstance(value.get("impression"), str) else None,
        "emotions": emotions,
        "updated_at": value.get("updated_at") if isinstance(value.get("updated_at"), str) else None,
        "processed_turn_id": _finite_number(value.get("processed_turn_id")),
    }


def event_relationship_evaluation(event: AgentEvent, payload: Mapping[str, JSONType]) -> dict[str, JSONType] | None:
    if event.event_type != "relationship_evaluation":
        return None
    succeeded = event.status == "succeeded"
    raw_changes = payload.get("changes")
    changes: dict[str, JSONType] = {}
    if succeeded and isinstance(raw_changes, Mapping):
        for key in _RELATIONSHIP_AXES:
            item = raw_changes.get(key)
            if isinstance(item, Mapping):
                changes[key] = {field: _finite_number(item.get(field)) for field in ("before", "after", "delta")}
    return {
        "event_ref": event.event_ref,
        "status": event.status,
        "evaluation_ref": payload.get("evaluation_ref") if isinstance(payload.get("evaluation_ref"), str) else None,
        "evidence_turn_ids": cast(JSONType, _turn_ids(payload.get("evidence_turn_ids"))),
        "before": relationship_snapshot(payload.get("before")),
        "after": relationship_snapshot(payload.get("after")) if succeeded else None,
        "changes": changes,
        **{
            key: payload.get(key) if isinstance(payload.get(key), str) else None
            for key in ("model", "error", "started_at", "finished_at", "queued_at")
        },
    }


def event_response_decision(event: AgentEvent, payload: Mapping[str, JSONType]) -> dict[str, JSONType] | None:
    if event.event_type != "response_decision":
        return None
    raw_delivery = payload.get("actual_delivery")
    delivery: dict[str, int | None] = {}
    for key in ("text_messages", "media_messages", "confirmed_deliveries"):
        count = raw_delivery.get(key) if isinstance(raw_delivery, Mapping) else None
        delivery[key] = count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else None
    outcome = payload.get("outcome")
    text_count, media_count = delivery["text_messages"], delivery["media_messages"]
    confirmed = delivery["confirmed_deliveries"]
    actual = "unknown"
    if confirmed is not None and confirmed > 0 and text_count is not None and media_count is not None:
        actual = (
            "mixed"
            if text_count > 0 and media_count > 0
            else "text"
            if text_count > 0
            else "media_only"
            if media_count > 0
            else "unknown"
        )
        if outcome == "declined" and text_count > 0:
            actual = "refusal"
    elif outcome == "silent" and confirmed == 0 and text_count == 0 and media_count == 0:
        actual = "silent"
    return {
        "event_ref": event.event_ref,
        "outcome": outcome if isinstance(outcome, str) else None,
        "source": payload.get("source") if isinstance(payload.get("source"), str) else None,
        "reason": payload.get("reason") if isinstance(payload.get("reason"), str) else None,
        "actual_delivery": cast(JSONType, delivery),
        "actual_outcome": actual,
    }


def project_relationship_views(events: Sequence[AgentEvent], *, compact: bool = False) -> dict[str, object]:
    """Project independent generation inputs, batch results and actual delivery choices."""
    views: dict[str, object] = {
        "relationship": None,
        "relationship_evaluation": None,
        "response_decision": None,
        "engagement": None,
    }
    for event in events:
        if event.event_type not in (
            "persona_state",
            "relationship_evaluation",
            "response_decision",
            "engagement_decision",
        ):
            continue
        payload = load_event_payload(event)
        if event.event_type == "persona_state":
            snapshot = relationship_snapshot(payload.get("relationship"))
            if compact and snapshot is not None:
                snapshot = {key: snapshot[key] for key in ("version", "axes", "emotions")}
                emotions = snapshot.get("emotions")
                if isinstance(emotions, list):
                    snapshot["emotions"] = [
                        {key: emotion[key] for key in ("name", "intensity")}
                        for emotion in emotions
                        if isinstance(emotion, dict)
                    ]
            views["relationship"] = snapshot
        elif event.event_type == "relationship_evaluation":
            evaluation = event_relationship_evaluation(event, payload)
            views["relationship_evaluation"] = (
                {key: evaluation[key] for key in ("event_ref", "status", "evaluation_ref", "changes")}
                | {"evidence_count": len(_turn_ids(payload.get("evidence_turn_ids")))}
                if compact and evaluation
                else evaluation
            )
        elif event.event_type == "response_decision":
            decision = event_response_decision(event, payload)
            views["response_decision"] = (
                {key: decision[key] for key in ("outcome", "actual_outcome", "actual_delivery")}
                if compact and decision
                else decision
            )
        else:
            engagement = event_engagement(event, payload)
            views["engagement"] = {"level_label": engagement["level_label"]} if compact and engagement else engagement
    return views


def event_images(
    event: AgentEvent, payload: Mapping[str, JSONType], *, output_only: bool = False
) -> list[dict[str, JSONType]]:
    """Project event-authorized private images into authenticated WebUI URLs."""

    images: list[dict[str, JSONType]] = []
    maximum = MAX_INPUT_AUDIT_ATTACHMENTS if event.event_type == "user_input" else _MAX_EVIDENCE_IMAGES
    for raw in event_attachment_metadata(cast(Mapping[str, object], payload))[:maximum]:
        attachment_ref = raw.get("attachment_ref")
        mime = raw.get("mime")
        recorded = is_agent_attachment(attachment_ref, mime)
        missing = raw.get("audit_status") == "unrecorded" or raw.get("status") == "unavailable"
        if not recorded and not missing:
            continue
        if (output_only or event.event_type == "message_delivery") and not str(attachment_ref).startswith("output_"):
            continue
        source = str(raw.get("source", "") or "")
        index = raw.get("index")
        ordinal = index if isinstance(index, int) and index > 0 else len(images) + 1
        configured_label = raw.get("label")
        if isinstance(configured_label, str) and configured_label.strip():
            label = configured_label.strip()
        elif str(attachment_ref).startswith("output_") or source in {"image_edit", "image_generation"}:
            label = f"\u751f\u6210\u7ed3\u679c {ordinal}"
        else:
            category = {
                "direct": "\u7528\u6237\u56fe\u7247",
                "quoted": "\u5f15\u7528\u56fe\u7247",
                "forward": "\u8f6c\u53d1\u56fe\u7247",
                "channel": "\u9891\u9053\u5386\u53f2\u56fe\u7247",
                "avatar": "\u53c2\u4e0e\u8005\u5934\u50cf",
                "persona": "\u89d2\u8272\u53c2\u8003\u56fe",
                "web": "\u7f51\u9875\u53c2\u8003\u56fe",
            }.get(
                source,
                "\u7f51\u9875\u53c2\u8003\u56fe"
                if str(attachment_ref).startswith("reference_")
                else "\u7528\u6237\u56fe\u7247",
            )
            label = f"{category} {ordinal}"
        description = raw.get("description")
        images.append(
            cast(
                dict[str, JSONType],
                {
                    "name": label,
                    "source": source,
                    "status": "recorded"
                    if recorded
                    else "unavailable"
                    if raw.get("status") == "unavailable"
                    else "unrecorded",
                    "mime": cast(str, mime),
                    "bytes": raw.get("bytes") if isinstance(raw.get("bytes"), int) else 0,
                    "text": description if isinstance(description, str) else "",
                    "url": (
                        f"{_INPUT_ATTACHMENT_ENDPOINT}/{quote(event.event_ref, safe='')}"
                        f"/attachments/{quote(cast(str, attachment_ref), safe='')}"
                        if recorded
                        else None
                    ),
                },
            )
        )
    return images


def serialize_event_view(event: AgentEvent, payload: Mapping[str, JSONType]) -> dict[str, object]:
    """Build the WebUI presentation fields for one durable event."""

    view: dict[str, object] = {
        "title": event_title(event),
        "preview": event_preview(event, payload),
        "details": event_details(event, payload),
        "arguments": _payload_section(payload, "arguments"),
        "result": _payload_section(payload, "result"),
        "evidence": event_evidence(payload),
        "persona": event_persona(event, payload),
        "engagement": event_engagement(event, payload),
        "relationship": relationship_snapshot(payload.get("relationship"))
        if event.event_type == "persona_state"
        else None,
        "relationship_evaluation": event_relationship_evaluation(event, payload),
        "response_decision": event_response_decision(event, payload),
        "images": event_images(event, payload),
        "payload_chars": len(event.payload_json or ""),
    }
    if event.event_type == "user_input":
        view["message"] = _message_projection(payload.get("content"))
    return view
