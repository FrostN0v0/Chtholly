"""Pure detection and control markers for requested media delivery."""

from __future__ import annotations

import re
import json
from typing import Literal
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from collections.abc import Mapping, Iterator, Sequence

from utils.request_text import request_clauses

from .types import ChatMessage

MEDIA_UNAVAILABLE_MARKER = "[MEDIA_UNAVAILABLE]"

_RENDER_TOOL = r"(?:html2pic|markdown2pic|md2pic|jinja2pic)"
_MEDIA_TERM = r"(?:图|图片|照片|表情包|贴纸|语音|音频|image|picture|photo|sticker|voice|audio)"
_IMAGE_OUTPUT_TERM = r"(?:图|图片|照片|形象|画面|插画|头像|海报|场景|\[图片\])"
_MEDIA_GENERATION_ACTION = r"(?:画(?!画)|绘制|生成|创作)"
_IMAGE_EDIT_ACTION = r"(?:消除|移除|删除|去掉|抹掉|擦除|替换|换掉|修改|调整|修正|编辑|重绘|重画)"
_NEGATED_MEDIA_REQUEST = re.compile(
    rf"(?:别|不要|不用|无需|禁止|不是(?:让|要)?).{{0,10}}"
    rf"(?:(?:发|传|贴|补|给|看).{{0,8}}{_MEDIA_TERM}|(?:用|以).{{0,4}}{_MEDIA_TERM})"
    rf"|(?:别|不要|不用|无需|禁止)\s*(?:再|继续)?\s*{_MEDIA_GENERATION_ACTION}\s*{_IMAGE_OUTPUT_TERM}"
    rf"|(?:别|不要|不用|无需|禁止).{{0,12}}{_RENDER_TOOL}"
    rf"|(?:do not|don't|dont|no need to).{{0,32}}{_RENDER_TOOL}"
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

_MEDIA_REQUEST_PATTERNS = (
    *_IMAGE_GENERATION_PATTERNS,
    _IMAGE_EDIT_PATTERN,
    re.compile(
        rf"(?:^|[。！？；;\n，])\s*(?:(?:请|帮我|给我|麻烦你|源码|预览|页面)\s*){{0,2}}"
        rf"(?:用|使用|通过)\s*{_RENDER_TOOL}\s*(?:发|给我|渲染|生成|展示)"
        rf"|(?:^|[.!?;\n])\s*(?:please\s+)?(?:send|render|show|convert).{{0,48}}"
        rf"(?:with|using|via)\s+{_RENDER_TOOL}"
        r"|(?:^|[。！？；;\n，])\s*(?:请|帮我)?\s*(?:把|将).{0,60}"
        r"(?:渲染|转成|转为|转换成)\s*(?:图片|图).{0,12}(?:发我|发给我|给我)",
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
_WEB_IMAGE_LOOKUP_TERM = (
    r"(?:搜索|搜一下|搜|查找|找一下|找(?:一|两|几)?(?:张|个)?|寻找|获取|从网上|网上|网页|网络|web|online|search|find)"
)
_WEB_IMAGE_REFERENCE_TERM = (
    r"(?:参考图|参照图|作为(?:视觉)?(?:参考|参照)|用作(?:视觉)?(?:参考|参照)|视觉(?:参考|参照)|"
    r"以.{0,16}为(?:参考|参照)|参考|照着|仿照|based on|reference)"
)
_WEB_IMAGE_REFERENCE_REQUEST = re.compile(
    rf"(?=.*{_WEB_IMAGE_LOOKUP_TERM})"
    r"(?=.*(?:图片|照片|立绘|形象|截图|image|picture|photo))"
    rf"(?=.*{_WEB_IMAGE_REFERENCE_TERM})"
    r"(?=.*(?:替换|换掉|编辑|修改|重绘|重画|生成|创作|绘制|画|edit|replace|redraw|generate|create|draw))",
    re.IGNORECASE,
)


MAX_IMAGE_REFERENCES = 4


@dataclass(frozen=True, slots=True)
class MediaIntent:
    """Host decisions derived only from the current unquoted user request."""

    media_requested: bool = False
    requires_source_edit: bool = False
    requires_web_reference: bool = False
    webpage_screenshot_requested: bool = False

    @property
    def requires_provenance(self) -> bool:
        return self.requires_source_edit or self.requires_web_reference


@dataclass(frozen=True, slots=True)
class ImageProvenance:
    """Evidence of the actual immutable pixels uploaded by an image tool."""

    kind: Literal["generated", "edited"]
    source_image_ref: str | None = None
    reference_image_refs: tuple[str, ...] = ()
    reference_sources: tuple[str, ...] = ()

    def satisfies(self, intent: MediaIntent) -> bool:
        if intent.requires_source_edit and (self.kind != "edited" or not self.source_image_ref):
            return False
        if len(self.reference_image_refs) != len(self.reference_sources):
            return False
        if intent.requires_web_reference and not any(
            ref and source == "web" for ref, source in zip(self.reference_image_refs, self.reference_sources)
        ):
            return False
        return True


@dataclass(slots=True)
class MediaDeliveryRequirements:
    intent: MediaIntent
    confirmed: bool = False

    def accepts(self, provenance: ImageProvenance | None) -> bool:
        return not self.intent.requires_provenance or (provenance is not None and provenance.satisfies(self.intent))


_MEDIA_REQUIREMENTS: ContextVar[MediaDeliveryRequirements | None] = ContextVar(
    "llm_chat_media_requirements", default=None
)


@contextmanager
def media_intent_scope(intent: MediaIntent) -> Iterator[MediaDeliveryRequirements]:
    requirements = MediaDeliveryRequirements(intent)
    token = _MEDIA_REQUIREMENTS.set(requirements)
    try:
        yield requirements
    finally:
        _MEDIA_REQUIREMENTS.reset(token)


def current_media_requirements() -> MediaDeliveryRequirements | None:
    return _MEDIA_REQUIREMENTS.get()


_DISCUSSION = re.compile(
    r"(?:如何|怎么|怎样|为什么|是什么|什么意思|教程|原理|能否介绍|是否支持|讨论|假如|如果|假设)"
    r"|\b(?:how (?:do|to|can)|what (?:is|does)|why|tutorial|discuss|suppose|if)\b",
    re.IGNORECASE,
)
_CANCEL = re.compile(
    r"^(?:算了|取消(?:吧|了)?|不必了|不用了|先别(?:做|弄)?了?|别做了|停止(?:吧)?)$"
    r"|^(?:never mind|nevermind|cancel(?: that)?|stop|do not do (?:it|that))$",
    re.IGNORECASE,
)
_NEGATED_ACTION = re.compile(
    r"(?:别|不要(?!只)|不用|无需|禁止|不是(?:让|要)?|不需要).{0,60}"
    r"(?:删|移除|消除|去掉|抹掉|擦除|替换|换|改|调整|修正|编辑|重绘|重画|画|生成|参考|搜索|截图|发)"
    r"|\b(?:do not|don't|dont|without|no need to|stop)\b",
    re.IGNORECASE,
)
_SOURCE_TARGET = re.compile(
    r"(?:这|那|原|源|上传|提供|刚才|群里).{0,12}(?:图|照片|头像)|头像|背景|底图|图中|图里|图片中"
    r"|\b(?:this|that|source|original|uploaded)\s+(?:image|picture|photo)|\b(?:avatar|background)\b",
    re.IGNORECASE,
)
_EDIT_COMMAND = re.compile(
    r"^(?:(?:请(?:你)?|帮我|给我|麻烦(?:你)?|能不能|可以|能否)\s*)*(?:把|将).{0,100}"
    r"(?:删(?:掉|除)?|移除|消除|去掉|抹掉|擦除|替换|换(?:掉|成)?|修改|调整|修正|编辑|重绘|重画|改成)"
    r"|^(?:(?:请(?:你)?|帮我|给我|麻烦(?:你)?|直接)\s*)*(?:删(?:掉|除)?|移除|消除|去掉|抹掉|擦除|替换|换(?:掉|成)?|修改|调整|修正|编辑|重绘|重画).{0,100}"
    r"|^(?:please\s+)?(?:edit|replace|remove|erase|change|redraw|modify)\b",
    re.IGNORECASE,
)


def build_media_intent(raw_user_text: str, *, has_image_inputs: bool = False) -> MediaIntent:
    """Parse once before enrichment; observations and historical text confer no rights."""

    media = edit = web = screenshot = False
    active_clauses: list[str] = []
    for clause in request_clauses(raw_user_text):
        if _CANCEL.search(clause):
            media = edit = web = screenshot = False
            active_clauses.clear()
            continue
        if _DISCUSSION.search(clause):
            continue
        if _NEGATED_ACTION.search(clause):
            if re.search(
                r"(?:别|不要(?!只)|不用|无需|不需要).{0,12}(?:画|绘制|生成|创作|发|发送)"
                r"|\b(?:do not|don't|dont|stop|no need to)\s+(?:send|show|share|draw|generate|create)\b",
                clause,
                re.IGNORECASE,
            ):
                media = edit = web = screenshot = False
                active_clauses.clear()
                continue
            if re.search(r"参考|搜索|网上|网页|\b(?:web|online|reference)\b", clause, re.IGNORECASE):
                web = False
                active_clauses.clear()
            if re.search(
                r"删|移除|消除|去掉|擦除|替换|换|改|编辑|重绘|重画|\b(?:edit|replace|remove|erase|change|redraw|modify)\b",
                clause,
                re.IGNORECASE,
            ):
                edit = False
            if _WEBPAGE_SCREENSHOT_NEGATION.search(clause):
                screenshot = False
            if _NEGATED_MEDIA_REQUEST.search(clause) or not (edit or web or screenshot):
                media = False
            continue
        active_clauses.append(clause)
        command = bool(_EDIT_COMMAND.search(clause))
        edit = edit or (command and (has_image_inputs or bool(_SOURCE_TARGET.search(clause))))
        screenshot = screenshot or bool(
            _WEBPAGE_SCREENSHOT_REQUEST.search(clause) and not _WEBPAGE_SCREENSHOT_REFERENCE.search(clause)
        )
        web = web or bool(_WEB_IMAGE_REFERENCE_REQUEST.search(" ".join(active_clauses)))
        media = (
            media
            or edit
            or screenshot
            or web
            or any(pattern.search(clause) for pattern in _MEDIA_REQUEST_PATTERNS if pattern is not _IMAGE_EDIT_PATTERN)
        )
    return MediaIntent(media, edit, web, screenshot)


def requests_contextual_media_delivery(raw_user_text: str, messages: Sequence[ChatMessage]) -> bool:
    """Use history only to resolve the target of an explicit current send request."""
    clauses = request_clauses(raw_user_text)
    if not clauses or not _CONTEXTUAL_MEDIA_DELIVERY.fullmatch(clauses[-1]):
        return False
    latest = next(
        (index for index in range(len(messages) - 1, -1, -1) if messages[index].get("role") == "user"), len(messages)
    )
    for message in reversed(messages[max(0, latest - 4) : latest]):
        if message.get("role") not in {"user", "assistant"}:
            continue
        text = _user_text(message.get("content"))
        if re.search(
            r"代码|源码|文本|文字|文件|脚本|报告|链接|\b(?:code|source|text|file|script|report|link)\b",
            text,
            re.IGNORECASE,
        ):
            return False
        if _RECENT_MEDIA_CONTEXT.search(text):
            return True
    return False


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


def latest_user_requests_media(messages: Sequence[ChatMessage]) -> bool:
    """Return a timeout hint only; enriched messages must never grant operation authority."""
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
    """Return a screenshot timeout hint from model-visible text, not an authorization grant."""

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
