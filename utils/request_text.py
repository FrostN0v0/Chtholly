"""Extract only current, unquoted request clauses from natural user text."""

from __future__ import annotations

import re
import json
from collections.abc import Mapping

_QUOTES = re.compile(
    r"```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`|\"[^\"\n]*\"|\u201c[^\u201d\n]*\u201d|"
    r"\u2018[^\u2019\n]*\u2019|\u300c[^\u300d\n]*\u300d|\u300e[^\u300f\n]*\u300f|"
    r"(?<!\w)'[^'\n]*'(?!\w)|^\s*>[^\n]*",
    re.MULTILINE,
)
_WRAPPER = re.compile(
    r"^\s*(?:\u5f15\u7528|\u8f6c\u53d1|quoted?|forwarded?|history)\s*[:\uff1a]|"
    r"<\s*/?\s*(?:quote|forward|history)\b",
    re.IGNORECASE,
)
_CLAUSES = re.compile(
    r"[\uff0c,\u3002\uff1b;\uff01!\uff1f?\n]+|\u4f46\u662f|\u4f46|\u800c\u662f|\u4e0d\u8fc7|"
    r"\b(?:but|however)\b",
    re.IGNORECASE,
)
_URL = re.compile(r"https?://[^\s\uff0c,\u3002\uff1b;\uff01!\uff1f?]+", re.IGNORECASE)


def request_clauses(raw: object) -> list[str]:
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
