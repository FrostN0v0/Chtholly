"""Detect escaped line breaks in decoded prose without rewriting literal content."""

from __future__ import annotations

import re

PROSE_LINEBREAK_GUIDANCE = (
    "Ordinary prose contains a literal backslash-n or backslash-r-backslash-n, not a line break. "
    "Use break segments or actual newline characters for layout; put intentional escape sequences in an "
    "explicit style=code segment or delimited inline/fenced code. Do not resend already confirmed content."
)
_LITERAL_LINEBREAK = re.compile(r"\\(?:r\\)?n")
_PROTECTED_LITERAL = re.compile(
    # Closed Markdown fences may end with a longer run of the same delimiter.
    r"(?m:^[ \t]{0,3}(?P<backtick_fence>`{3,})(?!`)[^\r\n]*\r?\n"
    r"[\s\S]*?^[ \t]{0,3}(?P=backtick_fence)`*[ \t]*(?=\r?$))"
    r"|(?m:^[ \t]{0,3}(?P<tilde_fence>~{3,})(?!~)[^\r\n]*\r?\n"
    r"[\s\S]*?^[ \t]{0,3}(?P=tilde_fence)~*[ \t]*(?=\r?$))"
    r"|(?<!`)(?P<ticks>`+)(?!`)[\s\S]*?(?<!`)(?P=ticks)(?!`)"
    # A scheme or an absolute/explicitly relative root distinguishes literals from prose.
    r"|[A-Za-z][A-Za-z0-9+.-]*://[^\s<>\"'`]+"
    r"""|(?P<quote>["'])(?:[A-Za-z]:[\\/]|\\\\[^\\/\s]+\\|\.{1,2}[\\/]|~[\\/]|/)[^\r\n]*?(?P=quote)"""
    r"|(?<![\w])(?:[A-Za-z]:[\\/]|\\\\[A-Za-z0-9._$-]+\\|\.{1,2}[\\/]|~[\\/]|/(?![\\/]))"
    r"[^\s<>\"'`]+"
    # An extension and a directory separator also identify a relative file path.
    r"|(?<![\w])[\w.-]+(?:[\\/][\w.-]+)*\\[\w.-]+\.[A-Za-z0-9]{1,12}(?![\w.])"
    r"|(?<![\w/])/(?:\\.|[^/\r\n])+/[a-z]*(?!\w)"
)


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
