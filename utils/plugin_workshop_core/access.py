"""Current-user intent required before workshop tool side effects."""

from __future__ import annotations

import re
from typing import Literal

from utils.request_text import request_clauses

from .models import WorkshopError

WorkshopAction = Literal["submit", "activate", "rollback"]
_PLUGIN = re.compile(r"\u63d2\u4ef6|\u6269\u5c55|\u529f\u80fd|\u547d\u4ee4|\b(?:plugin|extension|bot|command)\b", re.I)
_OPERATIONS = {
    "submit": re.compile(
        r"\u63d0\u4ea4|\u7f16\u5199|\u5199|\u751f\u6210|\u5f00\u53d1|\u5236\u4f5c|\u521b\u5efa|"
        r"\u5b9e\u73b0|\u4fee\u6539|\u66f4\u65b0|\u4fee\u590d|\u5b8c\u6210|\u6dfb\u52a0|\u505a|"
        r"\b(?:submit|write|generate|develop|build|create|implement|update|revise|fix|add)\b",
        re.I,
    ),
    "activate": re.compile(
        r"\u542f\u7528|\u6fc0\u6d3b|\u52a0\u8f7d|\u4e0a\u7ebf|\u90e8\u7f72|\b(?:activate|enable|load|deploy)\b", re.I
    ),
    "rollback": re.compile(
        r"\u56de\u6eda|\u56de\u9000|\u9000\u56de|\u6062\u590d|\b(?:rollback|roll\s+back|revert|restore)\b", re.I
    ),
}
_POLITE = re.compile(
    r"^(?:(?:\u5e2e\u6211|\u8bf7(?:\u4f60)?|\u9ebb\u70e6(?:\u4f60)?|\u80fd\u5426|\u53ef\u4ee5|"
    r"\u80fd\u4e0d\u80fd|\u90a3\u5c31|\u73b0\u5728|\u7ee7\u7eed|\u91cd\u65b0|\u76f4\u63a5|\u518d)\s*|"
    r"(?:please|can\s+you|could\s+you|would\s+you|then|now)\s+){0,4}",
    re.I,
)
_INDIRECT = re.compile(r"^(?:\u628a|\u5c06|\u7ed9|\u5bf9)|^(?:for|using)\b", re.I)
_NEGATIVE = re.compile(
    r"\u4e0d\u8981|\u522b|\u4e0d\u7528|\u65e0\u9700|\u4e0d\u9700\u8981|\u7981\u6b62|\u4e0d\u8bb8|"
    r"\u4e0d\u80fd|\u4e0d\u51c6|\u4e0d\u53ef\u4ee5|\u6ca1(?:\u6709)?(?:\u8ba9|\u8981\u6c42)|"
    r"\b(?:never|without|do\s+not|don't|dont|no\s+need\s+to|stop)\b",
    re.I,
)
_INSTRUCTIONAL = re.compile(
    r"\u5982\u4f55|\u600e\u4e48|\u600e\u6837|\u89e3\u91ca|\u793a\u4f8b|\u4f8b\u5982|\u5047\u8bbe|"
    r"\u5982\u679c|\u8bf4|\b(?:how|explain|example|tutorial|suppose|if|said|says)\b",
    re.I,
)
_REFERENCE = re.compile(
    r"\u5b83|\u8fd9\u4e2a|\u90a3\u4e2a|\u521a\u624d|\u4e0a\u4e00\u4e2a|\u4e4b\u524d|"
    r"\b(?:it|this|that|previous|earlier)\b",
    re.I,
)
_FOREIGN_NAME = re.compile(r"\b[a-z][a-z0-9_]{2,39}\b", re.I)
_GENERIC_NAMES = {
    "the",
    "plugin",
    "extension",
    "version",
    "revision",
    "this",
    "that",
    "previous",
    "earlier",
    "approved",
}


def _target_matches(subject: str, plugin_name: str, title: str) -> bool:
    if plugin_name and re.search(r"(?<![a-z0-9_])" + re.escape(plugin_name) + r"(?![a-z0-9_])", subject, re.I):
        return True
    if title and title.casefold() in subject.casefold():
        return True
    explicit_names = {
        word.casefold()
        for word in _FOREIGN_NAME.findall(subject)
        if word.casefold() not in _GENERIC_NAMES and not re.fullmatch(r"v\d+", word, re.I)
    }
    return not explicit_names and _REFERENCE.search(subject) is not None


def require_workshop_request(
    raw_user_text: object,
    action: WorkshopAction,
    *,
    plugin_name: str = "",
    title: str = "",
) -> None:
    """Do not derive authorization from tool parameters, quoted code, or history."""
    pattern = _OPERATIONS[action]
    requested = False
    for clause in request_clauses(raw_user_text):
        body = _POLITE.sub("", clause, count=1)
        operation = pattern.search(body)
        if operation is None:
            continue
        subject = body[: operation.start()] + body[operation.end() :]
        targeted = action == "submit" and _PLUGIN.search(subject) is not None
        if action != "submit":
            targeted = _target_matches(_NEGATIVE.sub("", subject), plugin_name, title)
        negative = _NEGATIVE.search(body[: operation.start()])
        if negative is not None and (targeted or not subject[negative.end() :].strip()):
            raise WorkshopError(
                "The current user excluded this workshop operation", code="authorization_required", status=403
            )
        if negative is not None or _INSTRUCTIONAL.search(body[: operation.start()]):
            continue
        if operation.start() and _INDIRECT.match(body) is None:
            continue
        requested |= targeted
    if not requested:
        raise WorkshopError(
            "An explicit current-user request for this plugin operation is required",
            code="authorization_required",
            status=403,
        )
