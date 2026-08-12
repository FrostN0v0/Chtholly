"""Pure detection and control markers for requested media delivery."""

from __future__ import annotations

import re
import json
from collections.abc import Mapping, Sequence

from .types import ChatMessage

MEDIA_UNAVAILABLE_MARKER = "[MEDIA_UNAVAILABLE]"

_MEDIA_TERM = r"(?:图|图片|照片|表情包|贴纸|语音|音频|image|picture|photo|sticker|voice|audio)"
_NEGATED_MEDIA_REQUEST = re.compile(
    rf"(?:别|不要|不用|无需|禁止|不是(?:让|要)?).{{0,10}}"
    rf"(?:(?:发|传|贴|补|给|看).{{0,8}}{_MEDIA_TERM}|(?:用|以).{{0,4}}{_MEDIA_TERM})"
    rf"|(?:do not|don't|dont|no need to|stop).{{0,12}}(?:send|show|share|use).{{0,8}}{_MEDIA_TERM}",
    re.IGNORECASE,
)
_MEDIA_REQUEST_PATTERNS = (
    re.compile(
        r"(?:^|[，。！？!?；;]\s*|(?:帮我|给我|请|那)\s*)"
        r"(?:画(?!画|法|风格|教程)|绘制|创作)\s*(?:一|两|几)?(?:张|幅|个)?\s*.{1,80}"
        r"|(?:^|[，。！？!?；;]\s*|(?:帮我|给我|请|那)\s*)"
        r"生成\s*(?!一?(?:个|份)?\s*(?:文字|文本|总结|报告|代码)).{1,80}"
        r"|(?:draw|generate|create)\s+(?:me\s+)?(?:an?\s+)?(?:image|picture|illustration)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:用|以)\s*(?:语音|音频|声音)\s*(?:说|讲|念|读|回复|回答|告诉)"
        r"|(?:说|讲|念|读)\s*(?:一|两|几)?(?:句|段|下)?\s*(?:语音|音频)"
        r"|(?:speak|say|read|reply|answer).{0,12}(?:by|in|with|using)\s+(?:voice|audio)",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?:来|发|传|贴|补|给我|让我|想看|看看|看下|瞧瞧).{{0,10}}"
        rf"(?:一|两|几)?(?:张|个|段)?\s*{_MEDIA_TERM}",
        re.IGNORECASE,
    ),
    re.compile(
        rf"{_MEDIA_TERM}.{{0,10}}(?:呢|在哪|哪里|没发|漏发|补上|发来|传来|给我|看看)",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?:send|show|share|resend).{{0,12}}(?:me\s+)?(?:an?\s+)?{_MEDIA_TERM}"
        rf"|where(?:'s| is).{{0,12}}(?:the\s+)?{_MEDIA_TERM}",
        re.IGNORECASE,
    ),
)


def _user_text(content: object) -> str:
    if not isinstance(content, str):
        return ""
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return content
    if not isinstance(payload, Mapping):
        return content
    nested = payload.get("content")
    return nested if isinstance(nested, str) else content


def latest_user_requests_media(messages: Sequence[ChatMessage]) -> bool:
    """Return whether the latest user turn explicitly requests media delivery."""

    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        text = _user_text(message.get("content")).strip()
        if not text or _NEGATED_MEDIA_REQUEST.search(text):
            return False
        return any(pattern.search(text) for pattern in _MEDIA_REQUEST_PATTERNS)
    return False


def is_media_unavailable_reply(text: str) -> bool:
    """Accept a marked unavailable reply only when it also contains visible text."""

    stripped = text.lstrip()
    if not stripped.startswith(MEDIA_UNAVAILABLE_MARKER):
        return False
    return bool(stripped.removeprefix(MEDIA_UNAVAILABLE_MARKER).strip())


def strip_media_unavailable_marker(text: str) -> str:
    """Remove the leading internal unavailable marker before user delivery."""

    stripped = text.lstrip()
    if not stripped.startswith(MEDIA_UNAVAILABLE_MARKER):
        return text
    return stripped.removeprefix(MEDIA_UNAVAILABLE_MARKER).lstrip()
