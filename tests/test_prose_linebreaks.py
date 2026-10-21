"""Behavioral boundaries for already-decoded prose and intentional literals."""

from __future__ import annotations

import json

import pytest

from plugins.llm_chat.core.prose_linebreaks import (
    PACKED_PROSE_GUIDANCE,
    has_packed_prose,
    prose_layout_error,
    has_literal_prose_linebreaks,
)


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


@pytest.mark.parametrize(
    "text",
    [
        "That makes me happy.\n\nPlease eat lunch; we can talk later.",
        "First beat.\nSecond beat.",
        "First beat.\r\n\r\nSecond beat.",
        "First beat.\u2028Second beat.",
        "First beat.\n\ufffc\nSecond beat.",
        "```text\nA code block.\n```\nFirst beat.\n\nSecond beat.",
        "- A list item.\nFirst beat.\n\nSecond beat.",
        "Name | Value\n--- | ---\na | b\n\nFirst beat.\nSecond beat.",
        "I like this | that.\nPlease eat lunch.",
    ],
)
def test_ordinary_prose_beats_require_separate_bubbles(text: str) -> None:
    assert has_packed_prose(text)
    assert prose_layout_error(text) == PACKED_PROSE_GUIDANCE


@pytest.mark.parametrize(
    "text",
    [
        "A short answer. Another sentence in the same beat.",
        "One detailed explanation stays in one line. " * 40,
        "A caption.\n\n\ufffc",
        "Here is the code:\n```python\nfirst()\n\nsecond()\n```\nIt uses the existing API.",
        "~~~text\nFirst paragraph in code.\n\nSecond paragraph in code.\n~~~",
        "The value is `first\nsecond` literally.",
        "Steps:\n- First item.\n  Wrapped continuation.\n\n- Second item.\nDone.",
        "1. First item.\n2. Second item.",
        "# Summary\nA complete explanation.\n## Details\nA second section.",
        (
            "# Report\nA first section paragraph.\n\nAnother paragraph explaining the same section."
            "\n## Details\nMore evidence."
        ),
        "Report\n======\nA first section paragraph.\n\nAnother section paragraph.",
        "Name | Value\n--- | :---:\na | b\nc | d",
        "| Name | Value |\n| --- | --- |\n| a | b |",
        "| Name | Value |\n| a | b |\n| c | d |",
        "+----+-----+\n| A  | B   |\n+----+-----+\n| 1  | 2   |\n+----+-----+",
        "┌───┬───┐\n│ A │ B │\n├───┼───┤\n│ 1 │ 2 │\n└───┴───┘",
        "> First quoted paragraph.\n>\n> Second quoted paragraph.\nMy response.",
        "    first()\n\n    second()",
    ],
)
def test_single_beats_and_deliberate_structured_layout_are_preserved(text: str) -> None:
    original = text.encode("utf-8")
    assert not has_packed_prose(text)
    assert prose_layout_error(text) is None
    assert text.encode("utf-8") == original
