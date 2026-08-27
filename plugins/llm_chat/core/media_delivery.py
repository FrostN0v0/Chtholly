"""Pure detection and control markers for requested media delivery."""

from __future__ import annotations

import re
import json
from collections.abc import Mapping, Sequence

from .types import ChatMessage

MEDIA_UNAVAILABLE_MARKER = "[MEDIA_UNAVAILABLE]"

_MEDIA_TERM = r"(?:图|图片|照片|表情包|贴纸|语音|音频|image|picture|photo|sticker|voice|audio)"
_IMAGE_OUTPUT_TERM = r"(?:图|图片|照片|形象|画面|插画|头像|海报|场景|\[图片\])"
_MEDIA_GENERATION_ACTION = r"(?:画(?!画)|绘制|生成|创作)"
_IMAGE_EDIT_ACTION = r"(?:消除|移除|删除|去掉|抹掉|擦除|替换|换掉|修改|调整|修正|编辑|重绘|重画)"
_NEGATED_MEDIA_REQUEST = re.compile(
    rf"(?:别|不要|不用|无需|禁止|不是(?:让|要)?).{{0,10}}"
    rf"(?:(?:发|传|贴|补|给|看).{{0,8}}{_MEDIA_TERM}|(?:用|以).{{0,4}}{_MEDIA_TERM})"
    rf"|(?:别|不要|不用|无需|禁止)\s*(?:再|继续)?\s*{_MEDIA_GENERATION_ACTION}\s*{_IMAGE_OUTPUT_TERM}"
    rf"|(?:do not|don't|dont|no need to|stop).{{0,12}}(?:send|show|share|use).{{0,8}}{_MEDIA_TERM}",
    re.IGNORECASE,
)
_IMAGE_GENERATION_PATTERNS = (
    re.compile(
        r"(?:^|[，。！？!?；;]\s*|(?:帮我|给我|请|那)\s*)"
        r"(?:画(?!画|法|风格|教程)|绘制|创作)\s*(?:一|两|几)?(?:张|幅|个)?\s*.{1,80}"
        r"|(?:^|[，。！？!?；;]\s*|(?:帮我|给我|请|那)\s*)"
        r"生成\s*(?!一?(?:个|份)?\s*(?:文字|文本|总结|报告|代码)).{1,80}"
        r"|(?:draw|generate|create)\s+(?:me\s+)?(?:an?\s+)?(?:image|picture|illustration)",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?=.{{0,160}}{_MEDIA_GENERATION_ACTION})(?=.{{0,160}}{_IMAGE_OUTPUT_TERM})"
        r"(?:不要只|别只|不能只|重新|再|继续|改成|参考|根据|按照|按|用我|用这个|自己).{0,160}"
        rf"|{_MEDIA_GENERATION_ACTION}.{{0,100}}"
        rf"(?:重新|再|继续|改成|参考|根据|按照|按|用我|用这个|自己).{{0,100}}{_IMAGE_OUTPUT_TERM}",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?:仿照|参照|参考|照着|按照|按).{{0,100}}(?:生成|做|画|绘制|创作).{{0,100}}{_IMAGE_OUTPUT_TERM}"
        rf"|(?:仿照|参照|参考|照着|按照|按).{{0,100}}{_IMAGE_OUTPUT_TERM}.{{0,100}}(?:生成|做|画|绘制|创作)",
        re.IGNORECASE,
    ),
)
_IMAGE_EDIT_PATTERN = re.compile(
    rf"(?=.{{0,160}}{_IMAGE_OUTPUT_TERM})(?:把|将|帮我|请|给我)?.{{0,100}}{_IMAGE_EDIT_ACTION}.{{0,80}}",
    re.IGNORECASE,
)
_SELF_IMAGE_REQUEST_PATTERN = re.compile(
    rf"(?=.{{0,160}}(?:你(?:自己|的)?|自己|珂朵莉|chtholly))(?=.{{0,160}}{_IMAGE_OUTPUT_TERM})"
    rf"(?:来|发|传|给我|让我看|想看|看看|看下|瞧瞧|{_IMAGE_EDIT_ACTION}).{{0,160}}"
    r"|(?:show|send|share).{0,32}(?:image|picture|photo).{0,32}(?:of\s+)?(?:you|yourself)",
    re.IGNORECASE,
)
_MEDIA_REQUEST_PATTERNS = (
    *_IMAGE_GENERATION_PATTERNS,
    _IMAGE_EDIT_PATTERN,
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

_CONTEXTUAL_MEDIA_DELIVERY = re.compile(
    r"^(?:那|所以|这个|那个|它|他|她|你)?\s*(?:能|可以|可不可以|能不能)?\s*(?:直接)?\s*"
    r"(?:把(?:它|这个|那个|原图|头像))?\s*(?:发|传|贴|补)(?:出|过|来|给我)*(?:一下)?(?:吗|么|吧)?[？?]?$",
    re.IGNORECASE,
)
_RECENT_MEDIA_CONTEXT = re.compile(rf"(?:{_MEDIA_TERM}|头像|原图|画面|插画|海报)", re.IGNORECASE)
_WEBPAGE_SCREENSHOT_ACTION = r"(?:截图|截屏|截(?:个|一张|一下)(?:图|屏)?|截)"
_WEBPAGE_SCREENSHOT_NEGATION = re.compile(
    rf"(?:别|不要|不用|无需|禁止|不是(?:让|要)?).{{0,24}}(?:{_WEBPAGE_SCREENSHOT_ACTION}"
    r"|(?:(?:网页|页面|网站|网址|链接).{0,16}(?:截图|截屏)"
    r"|(?:截图|截屏).{0,16}(?:网页|页面|网站|网址|链接)))"
    r"|(?:do not|don't|dont|no need to|without).{0,24}(?:screenshot|screen shot|capture).{0,24}"
    r"(?:webpage|web page|page|site|website|url|link)",
    re.IGNORECASE,
)
_WEBPAGE_SCREENSHOT_REFERENCE = re.compile(
    rf"{_WEBPAGE_SCREENSHOT_ACTION}\s*(?:一下\s*)?"
    r"(?:里|中|上|内|内容|是什么|有什么|怎么|如何|工具|软件|方法|教程|识别|分析)",
    re.IGNORECASE,
)
_WEBPAGE_SCREENSHOT_PREFIX = (
    r"^\s*(?:<at\b[^>]*?/?>\s*)*"
    r"(?:(?:帮我|请(?:你)?|给我|把|将|麻烦(?:你)?|能否|可以|可不可以|能不能)\s*)?"
)
_WEBPAGE_SCREENSHOT_REQUEST = re.compile(
    r"(?:帮我|请(?:你)?|给我|把|将|麻烦(?:你)?|能否|可以|可不可以|能不能).{0,20}"
    r"(?:(?:这个|该|当前|上面)?(?:网页|页面|网站|网址|链接).{0,20}"
    r"(?:截图|截屏|截(?:个|一张|一下)?(?:图|屏)?)"
    r"|(?:截图|截屏|截(?:个|一张|一下)?(?:图|屏)?).{0,20}"
    r"(?:这个|该|当前|上面)?(?:网页|页面|网站|网址|链接))"
    r"|^\s*(?:截图|截屏|截(?:个|一张|一下)?(?:图|屏)?).{0,20}"
    r"(?:这个|该|当前|上面)?(?:网页|页面|网站|网址|链接)"
    r"(?:\s*(?:发|传|给)(?:给)?我|\s*(?:一下|吧|吗|么))?[。！？!?]?\s*$"
    rf"|{_WEBPAGE_SCREENSHOT_PREFIX}{_WEBPAGE_SCREENSHOT_ACTION}"
    r"(?:\s*(?:一下|吧|给我|发给我|传给我))?\s*[。！？!?]?\s*$"
    rf"|{_WEBPAGE_SCREENSHOT_PREFIX}{_WEBPAGE_SCREENSHOT_ACTION}\s*(?:一下\s*)?"
    r"(?!(?:里|中|上|内|内容|是什么|有什么|怎么|如何|工具|软件|方法|教程|识别|分析))"
    r".{1,100}[。！？!?]?\s*$"
    r"|(?:take|send|show|share|capture).{0,16}(?:an?\s+)?(?:screenshot|screen shot).{0,24}"
    r"(?:of\s+)?(?:this\s+|the\s+)?(?:webpage|web page|page|site|website|url|link)"
    r"|(?:screenshot|screen shot).{0,16}(?:this\s+|the\s+)?"
    r"(?:webpage|web page|page|site|website|url|link)(?:.{0,16}(?:for|to)\s+me)?",
    re.IGNORECASE,
)


def _user_text(content: object) -> str:
    if isinstance(content, str):
        try:
            payload = json.loads(content)
        except (TypeError, ValueError):
            return content
        if not isinstance(payload, Mapping):
            return content
        nested = payload.get("content")
        return nested if isinstance(nested, str) else content
    if isinstance(content, Sequence):
        text_parts: list[str] = []
        for part in content:
            if not isinstance(part, Mapping) or part.get("type") != "text":
                continue
            text = part.get("text")
            if isinstance(text, str):
                text_parts.append(_user_text(text))
        return " ".join(text_parts)
    return ""


def latest_user_requests_image_generation(messages: Sequence[ChatMessage]) -> bool:
    """Return whether the latest turn may need the trusted self image reference."""

    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        text = _user_text(message.get("content")).strip()
        if not text or _NEGATED_MEDIA_REQUEST.search(text):
            return False
        return any(pattern.search(text) for pattern in _IMAGE_GENERATION_PATTERNS) or bool(
            _SELF_IMAGE_REQUEST_PATTERN.search(text)
        )
    return False


def latest_user_requests_media(messages: Sequence[ChatMessage]) -> bool:
    """Return whether the latest user turn requests media directly or by recent reference."""
    if latest_user_requests_webpage_screenshot(messages):
        return True

    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.get("role") != "user":
            continue
        text = _user_text(message.get("content")).strip()
        if not text or _NEGATED_MEDIA_REQUEST.search(text):
            return False
        if any(pattern.search(text) for pattern in _MEDIA_REQUEST_PATTERNS):
            return True
        if not _CONTEXTUAL_MEDIA_DELIVERY.search(text):
            return False
        recent_context = messages[max(0, index - 4) : index]
        return any(_RECENT_MEDIA_CONTEXT.search(_user_text(item.get("content"))) for item in recent_context)
    return False


def latest_user_requests_webpage_screenshot(messages: Sequence[ChatMessage]) -> bool:
    """Return whether the latest user explicitly requests a webpage screenshot."""

    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        text = _user_text(message.get("content")).strip()
        if not text or _WEBPAGE_SCREENSHOT_NEGATION.search(text) or _WEBPAGE_SCREENSHOT_REFERENCE.search(text):
            return False
        return bool(_WEBPAGE_SCREENSHOT_REQUEST.search(text))
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
