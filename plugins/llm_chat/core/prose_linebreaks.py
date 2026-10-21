"""Detect invalid decoded prose layout without rewriting literal content."""

from __future__ import annotations

import re

from .media import has_meaningful_text

PROSE_LINEBREAK_GUIDANCE = (
    "Ordinary prose contains a literal backslash-n or backslash-r-backslash-n, not a line break. "
    "Use actual newlines or break segments only for deliberate layout within one conversational beat; send "
    "separate beats with separate send_msg calls. Put intentional escape sequences in an explicit style=code "
    "segment or delimited inline/fenced code. Do not resend already confirmed content."
)
PACKED_PROSE_GUIDANCE = (
    "This message packs multiple ordinary prose paragraphs or lines into one chat bubble. "
    "Send each natural conversational beat with a separate send_msg call; newlines, break segments and "
    "non-code styles do not create separate messages. Do not flatten or silently split the text. "
    "Keep deliberate lists, code, tables, quotes and other structured layout intact, and keep a short "
    "single-beat reply together. Text and an image may share a call when they belong to that same beat. "
    "Only correct unsent content; never resend confirmed or unknown deliveries."
)
# A validation-only projection of an explicitly typed code segment, never emitted.
CODE_LAYOUT_MARKER = "\ufff9"
_LITERAL_LINEBREAK = re.compile(r"\\(?:r\\)?n")
_DELIMITED_CODE = re.compile(
    # Closed Markdown fences may end with a longer run of the same delimiter.
    r"(?m:^[ \t]{0,3}(?P<backtick_fence>`{3,})(?!`)[^\r\n]*\r?\n"
    r"[\s\S]*?^[ \t]{0,3}(?P=backtick_fence)`*[ \t]*(?=\r?$))"
    r"|(?m:^[ \t]{0,3}(?P<tilde_fence>~{3,})(?!~)[^\r\n]*\r?\n"
    r"[\s\S]*?^[ \t]{0,3}(?P=tilde_fence)~*[ \t]*(?=\r?$))"
    r"|(?<!`)(?P<ticks>`+)(?!`)[\s\S]*?(?<!`)(?P=ticks)(?!`)"
)
_PROTECTED_LITERAL = re.compile(
    _DELIMITED_CODE.pattern
    # A scheme or an absolute/explicitly relative root distinguishes literals from prose.
    + r"|[A-Za-z][A-Za-z0-9+.-]*://[^\s<>\"'`]+"
    r"""|(?P<quote>["'])(?:[A-Za-z]:[\\/]|\\\\[^\\/\s]+\\|\.{1,2}[\\/]|~[\\/]|/)[^\r\n]*?(?P=quote)"""
    r"|(?<![\w])(?:[A-Za-z]:[\\/]|\\\\[A-Za-z0-9._$-]+\\|\.{1,2}[\\/]|~[\\/]|/(?![\\/]))"
    r"[^\s<>\"'`]+"
    # An extension and a directory separator also identify a relative file path.
    r"|(?<![\w])[\w.-]+(?:[\\/][\w.-]+)*\\[\w.-]+\.[A-Za-z0-9]{1,12}(?![\w.])"
    r"|(?<![\w/])/(?:\\.|[^/\r\n])+/[a-z]*(?!\w)"
)
_LIST_LINE = re.compile(r"^(?:[-+*•][ \t]+|\d+[.)][ \t]+|\d+、|[一二三四五六七八九十]+、)")
_SECTIONED_DOCUMENT = re.compile(
    r"\A[ \t\r\n]*(?:#{1,6}[ \t]+\S|[^\r\n]+\r?\n[ \t]*(?:={3,}|-{3,})[ \t]*(?=\r?\n|\Z))"
)
_STRUCTURED_LINE = re.compile(
    r"^(?:#{1,6}[ \t]+|>[ \t]?)"
    r"|^(?:[-*_][ \t]*){3,}$"
    r"|^={3,}$"
    r"|^\+(?:[-=]+\+)+$"
    r"|^[┌┬┐├┼┤└┴┘─━┏┳┓┣╋┫┗┻┛]+$"
)
_TABLE_DIVIDER = re.compile(r"^\|?[ \t]*:?-{3,}:?[ \t]*(?:\|[ \t]*:?-{3,}:?[ \t]*)+\|?$")


def has_packed_prose(text: str) -> bool:
    """Detect adjacent ordinary prose beats, not deliberate structured layout.

    Segment callers must concatenate text and non-code styles, project explicit
    breaks as newlines, code as CODE_LAYOUT_MARKER, and other native elements as
    object replacement characters. Blank lines and media never reset the prose
    run, so segment boundaries cannot hide another conversational paragraph.
    Structured blocks separate their own introduction and explanation. An
    explicitly headed document keeps its section paragraphs; a small list or
    code block does not exempt unrelated prose elsewhere. This is not a splitter.
    """

    if not any(char in text for char in "\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029"):
        return False
    if _SECTIONED_DOCUMENT.match(text):
        return False
    lines = _DELIMITED_CODE.sub(CODE_LAYOUT_MARKER, text).splitlines()
    ordinary_lines = 0
    list_continuation = False
    table = False
    for index, raw_line in enumerate(lines):
        line = raw_line.replace("\ufffc", "").strip()
        if not line:
            table = False
            continue
        if "|" not in line:
            table = False
        elif _TABLE_DIVIDER.fullmatch(line) or (
            index + 1 < len(lines) and _TABLE_DIVIDER.fullmatch(lines[index + 1].strip())
        ):
            table = True
        structured = (
            _STRUCTURED_LINE.search(line) is not None
            or _LIST_LINE.match(line) is not None
            or table
            or (line.startswith(("|", "│", "┃")) and line.endswith(("|", "│", "┃")))
            or line == CODE_LAYOUT_MARKER
            or raw_line.startswith(("    ", "\t"))
            or (list_continuation and raw_line.startswith("  "))
        )
        if structured:
            ordinary_lines = 0
            list_continuation = _LIST_LINE.match(line) is not None or (
                list_continuation and raw_line.startswith(("  ", "\t"))
            )
            continue
        list_continuation = False
        if has_meaningful_text(line.replace(CODE_LAYOUT_MARKER, "")):
            ordinary_lines += 1
            if ordinary_lines > 1:
                return True
    return False


def prose_layout_error(text: str) -> str | None:
    """Return actionable layout guidance without changing any authored bytes."""

    if has_literal_prose_linebreaks(text):
        return PROSE_LINEBREAK_GUIDANCE
    if has_packed_prose(text):
        return PACKED_PROSE_GUIDANCE
    return None


def has_literal_prose_linebreaks(text: str) -> bool:
    """Reject ambiguous escapes outside delimited code and recognizable paths/URLs.

    The input is already JSON-decoded. Actual LF/CRLF and every permitted literal
    are left untouched; this is a predicate, never an escape decoder. Callers must
    exclude explicitly typed code segments from their prose projection.
    """

    candidate = _LITERAL_LINEBREAK.search(text)
    if candidate is None:
        return False
    for literal in _PROTECTED_LITERAL.finditer(text):
        if candidate.start() < literal.start():
            return True
        if candidate.start() < literal.end():
            candidate = _LITERAL_LINEBREAK.search(text, literal.end())
            if candidate is None:
                return False
    return True
