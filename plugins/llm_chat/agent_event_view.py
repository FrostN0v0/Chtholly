"""Human-readable projections of durable AgentEvent rows for the WebUI."""

from __future__ import annotations

import json
from typing import cast
from urllib.parse import quote
from collections.abc import Mapping, Sequence

from .models import AgentEvent
from .core.types import JSONType

_INLINE_OBJECT_CHARS = 8000
_PREVIEW_CHARS = 400
_DETAIL_VALUE_CHARS = 200
_MAX_DETAILS = 8
_MAX_EVIDENCE_IMAGES = 12
_MEME_FILE_ENDPOINT = "/api/llm-chat/memes/files"

EVENT_TITLES = {
    "user_input": "用户输入",
    "model_attempt": "模型调用",
    "assistant_tool_call": "工具调用",
    "tool_result": "工具结果",
    "assistant_output": "最终回复",
    "context_selection": "上下文选择",
    "persona_state": "人格与记忆",
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
_SKIPPED_DETAIL_KEYS = frozenset({"content", "context_arguments", "context_result", "metrics", "evidence"})


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
    if key not in payload:
        return None
    value = payload[key]
    if value is None:
        return None
    serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(serialized) > _INLINE_OBJECT_CHARS:
        return {"stored": True, "chars": len(serialized)}
    return value


def _preview_source(event: AgentEvent, payload: Mapping[str, JSONType]) -> Mapping[str, JSONType]:
    if event.event_type == "assistant_tool_call":
        arguments = payload.get("arguments")
        return arguments if isinstance(arguments, Mapping) else payload
    if event.event_type == "tool_result":
        result = payload.get("result")
        return result if isinstance(result, Mapping) else payload
    return payload


def _unwrap_user_turn(value: str) -> str:
    """Return the human-readable text inside one serialized user turn."""

    candidate = value.strip()
    if not candidate.startswith("{"):
        return value
    try:
        parsed = json.loads(candidate)
    except ValueError:
        return value
    if not isinstance(parsed, Mapping):
        return value
    inner = parsed.get("content")
    speaker = parsed.get("speaker")
    if not isinstance(inner, str):
        return value
    return f"{speaker}：{inner}" if isinstance(speaker, str) and speaker else inner


def event_preview(event: AgentEvent, payload: Mapping[str, JSONType]) -> str:
    """Return the most informative plain-text content of one event."""

    source = _preview_source(event, payload)
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
        return _compact(_collection_text(source, _PREVIEW_CHARS), _PREVIEW_CHARS)
    return ""


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


def serialize_event_view(event: AgentEvent, payload: Mapping[str, JSONType]) -> dict[str, object]:
    """Build the WebUI presentation fields for one durable event."""

    return {
        "title": event_title(event),
        "preview": event_preview(event, payload),
        "details": event_details(event, payload),
        "arguments": _payload_section(payload, "arguments"),
        "result": _payload_section(payload, "result"),
        "evidence": event_evidence(payload),
        "persona": event_persona(event, payload),
        "payload_chars": len(event.payload_json or ""),
    }
