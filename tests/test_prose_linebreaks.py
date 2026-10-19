"""Behavioral boundaries for already-decoded prose and intentional literals."""

from __future__ import annotations

import json

import pytest

from plugins.llm_chat.core.prose_linebreaks import has_literal_prose_linebreaks


@pytest.mark.parametrize(
    "text",
    [
        "first line\nsecond line",
        "first line\r\nsecond line",
        "\u7b2c\u4e00\u884c\n\u7b2c\u4e8c\u884c",
        r"Use `\n` to match a newline.",
        r"Use ``a `backtick` and \r\n`` literally.",
        "```python\npattern = r'\\r?\\n'\n```",
        "~~~regex\n\\r?\\n\n~~~",
        "```python\npattern = r'\\n'\n````",
        "~~~regex\n\\n\n~~~~",
        r"Regex /\r?\n/g is intentional.",
        r"Open C:\new\notes.txt next.",
        r"Open logs\new\notes.txt next.",
        r"Open notes\new.txt next.",
        r"Open C:/new\notes.txt next.",
        r"Open \\server\share\new\notes.txt next.",
        r"Open .\new\notes.txt next.",
        r"Open ../new\notes.txt next.",
        r"Open ~/new\notes.txt next.",
        r"Open /tmp/new\notes.txt next.",
        r'Open "C:\Program Files\new\notes.txt" next.',
        r'Open "\\server\shared folder\new\notes.txt" next.',
        r"See https://example.com/new\notes?q=\n next.",
        r"See file:///C:/new\notes.txt next.",
        r"An unrelated \t escape is not a line-break policy violation.",
        "[END_OF_RESPONSE]",
    ],
)
def test_actual_newlines_and_explicit_literals_are_not_malformed(text: str) -> None:
    assert not has_literal_prose_linebreaks(text)


@pytest.mark.parametrize(
    "text",
    [
        r"first line\nsecond line",
        r"first line\r\nsecond line",
        r"first line\\nsecond line",
        "\u7b2c\u4e00\u884c\\n\u7b2c\u4e8c\u884c",
        r"A newline is \n here.",
        r"`\n` is intentional, but this prose\nis not.",
        r"C:\new\notes.txt is literal; this prose\nis not.",
        r"https://example.com/ is literal; this prose\ris malformed\nnext.",
        "```python\npattern = r'\\n'\n```\nordinary prose\\nnext",
        "~~~regex\n\\n\n~~~\nordinary prose\\r\\nnext",
        r"Unclosed `code\n is ambiguous.",
    ],
)
def test_ambiguous_prose_escapes_are_detected_without_decoding(text: str) -> None:
    assert has_literal_prose_linebreaks(text)


def test_json_decoding_not_json_source_spelling_defines_the_boundary() -> None:
    actual = json.loads(r'{"text":"first\nsecond"}')["text"]
    literal = json.loads(r'{"text":"first\\nsecond"}')["text"]
    crlf_literal = json.loads(r'{"text":"first\\r\\nsecond"}')["text"]

    assert actual == "first\nsecond"
    assert literal == r"first\nsecond"
    assert crlf_literal == r"first\r\nsecond"
    assert not has_literal_prose_linebreaks(actual)
    assert has_literal_prose_linebreaks(literal)
    assert has_literal_prose_linebreaks(crlf_literal)
