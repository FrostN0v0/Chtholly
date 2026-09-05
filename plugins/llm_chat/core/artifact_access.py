"""Optional artifact routing hints and explicit destructive-revocation intent."""

from __future__ import annotations

import re
import json
from typing import Literal
from collections.abc import Mapping


class ArtifactAccessError(ValueError):
    """The current user has not requested destructive artifact revocation."""


_WEB = re.compile(
    r"(?:网页|网站|页面|界面)(?!截图|图片|照片)|前端|原型|导航栏|侧边栏|按钮|标签页|"
    r"(?<![a-z0-9_])(?:ui|html|css|web\s*(?:page|site|app)|website|webpage|prototype|landing\s+page|"
    r"dashboard|navbar|sidebar|button|modal|tabs?)(?![a-z0-9_]|\s+(?:screenshot|image|photo)\b)",
    re.IGNORECASE,
)
_ARTIFACT = re.compile(
    r"(?:网页|网站|页面|界面)(?!截图|图片|照片)|前端|原型|预览|源码|源代码|源文件|压缩包|文件|项目|版本|链接|"
    r"(?<![a-z0-9_])(?:ui|html|css|website|webpage|web\s*(?:page|site|app)|prototype|preview|source|zip|"
    r"archive|file|project|artifact|version|revision|link|url)(?![a-z0-9_])",
    re.IGNORECASE,
)
_OPERATIONS: dict[str, str] = {
    "create": (
        r"制作|创建|设计|实现|开发|搭建|生成|构建|修改|重构|更新|重做|改成|改为|做成|弄个|来个|做|写|改|"
        r"\b(?:create|build|rebuild|design|redesign|make|implement|develop|write|generate|update|change|edit|refactor)\b"
    ),
    "publish": r"发布|上线|托管|预览|公开|\b(?:publish|host|deploy|preview)\b",
    "send": (
        r"发送|发(?!布)|传|给我|提供|交付|下载|导出|打包|"
        r"\b(?:send|give|share|provide|deliver|download|export|attach|upload)\b"
    ),
    "revoke": r"撤销|作废|废止|下线|删除|取消|回收|\b(?:revoke|invalidate|disable|delete|unpublish)\b",
}
_PATTERNS = {name: re.compile(pattern, re.IGNORECASE) for name, pattern in _OPERATIONS.items()}
_POLITE = re.compile(
    r"^(?:(?:帮我|请(?:你)?|麻烦(?:你)?|能否|可以|可不可以|能不能|那就|现在|继续|重新|直接|再)\s*|"
    r"(?:please|can\s+you|could\s+you|would\s+you|then|now)\s+){0,3}",
    re.IGNORECASE,
)
_INDIRECT = re.compile(r"^(?:把|将|参考|根据|按照|按|以|用)|^(?:using|based\s+on)\b", re.IGNORECASE)
_QUERY = re.compile(r"如何|怎么|怎样|解释|说明如何|\b(?:how\s+to|what\s+is|explain|tutorial)\b", re.IGNORECASE)
_NEGATION = re.compile(
    r"不要(?!只)|别(?!只)|不用|无需|不需要|禁止|不许|拒绝|不能|不准|不可以|没(?:有)?(?:让|要求)|不是(?:让|要)|"
    r"\b(?:never|without|do\s+not|don't|dont|no\s+need\s+to|stop)\b(?!\s+just\b)",
    re.IGNORECASE,
)
_QUOTES = re.compile(
    r"```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`|\"[^\"\n]*\"|“[^”\n]*”|‘[^’\n]*’|"
    r"「[^」\n]*」|『[^』\n]*』|(?<!\w)'[^'\n]*'(?!\w)|^\s*>[^\n]*",
    re.MULTILINE,
)
_WRAPPER = re.compile(
    r"^\s*(?:引用|转发|quoted?|forwarded?|history)\s*[:：]|<\s*/?\s*(?:quote|forward|history)\b",
    re.IGNORECASE,
)
_CLAUSES = re.compile(r"[，,。；;！!？?\n]+|但是|但|而是|不过|\b(?:but|however)\b", re.IGNORECASE)
_EMPTY_OBJECT = re.compile(r"^(?:\s|了|吧|它|这个|那个|再|\b(?:it|this|that|them|again)\b)*$", re.IGNORECASE)
_URL = re.compile(r"https?://[^\s，,。；;！!？?]+", re.IGNORECASE)
_PUBLICATION_NOUN = re.compile(r"\b(?:publishing|publication|hosting|deployment|deploying|previewing)\b", re.IGNORECASE)


def _clauses(raw: object) -> list[str]:
    if not isinstance(raw, str) or _WRAPPER.search(raw):
        return []
    try:
        structured = json.loads(raw)
    except (TypeError, ValueError):
        structured = None
    if isinstance(structured, (Mapping, list)):
        return []
    unquoted = _URL.sub(" ", _QUOTES.sub(" ", raw))
    return [part.strip() for part in _CLAUSES.split(unquoted) if part.strip()]


def _requests(clause: str, operation: str, *, web_only: bool = False) -> bool:
    body = _POLITE.sub("", clause, count=1)
    pattern = _PATTERNS[operation]
    if body.startswith("给我") and pattern.match(body[2:].lstrip()):
        body = body[2:].lstrip()
    match = pattern.search(body) if _INDIRECT.match(body) else pattern.match(body)
    if match is None or _QUERY.search(body[: match.start()]):
        return False
    if _NEGATION.search(body[: match.start()]):
        return False
    subject = body[match.end() :]
    if body.startswith(("把", "将")):
        subject = body[: match.start()] + subject
    return bool((_WEB if web_only else _ARTIFACT).search(subject))


def _negates(clause: str, operation: str) -> bool:
    for negative in _NEGATION.finditer(clause):
        body = clause[negative.end() :]
        if operation == "publish" and _PUBLICATION_NOUN.search(body):
            return True
        match = _PATTERNS[operation].search(body)
        if match is None or match.start() > 80:
            continue
        if operation == "publish":
            return True
        subject = body[: match.start()] + body[match.end() :]
        if (_WEB if operation == "create" else _ARTIFACT).search(subject) or _EMPTY_OBJECT.fullmatch(subject):
            return True
    return False


def is_artifact_request(raw_user_text: object, action: Literal["publish", "send"] = "publish") -> bool:
    """Suggest media routing; a false result never denies an artifact tool."""

    clauses = _clauses(raw_user_text)
    operations = ("create", "publish") if action == "publish" else (action,)
    if any(_negates(clause, operation) for clause in clauses for operation in operations):
        return False
    return any(_requests(clause, "create", web_only=True) or _requests(clause, action) for clause in clauses)


def require_artifact_revocation(raw_user_text: object) -> None:
    """Require an affirmative current-user request before invalidating a link."""

    clauses = _clauses(raw_user_text)
    if not clauses or any(_negates(clause, "revoke") for clause in clauses):
        raise ArtifactAccessError("current user must explicitly request artifact revocation")
    if not any(_requests(clause, "revoke") for clause in clauses):
        raise ArtifactAccessError("current user must explicitly request artifact revocation")
