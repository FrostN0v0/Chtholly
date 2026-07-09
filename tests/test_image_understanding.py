"""Unit tests for inbound image description normalization and note formatting."""

from utils.llm_chat_core.media import (
    format_image_note,
    normalize_image_description,
)


class TestNormalizeImageDescription:
    def test_collapses_newlines_and_runs_of_whitespace(self):
        text = "一张  截图\n\n主体是猫\t正在打字"
        assert normalize_image_description(text) == "一张 截图 主体是猫 正在打字"

    def test_over_limit_truncates_to_limit_with_ellipsis(self):
        result = normalize_image_description("a" * 150)
        assert len(result) == 100
        assert result == "a" * 99 + "…"

    def test_exactly_limit_is_not_truncated(self):
        text = "b" * 100
        assert normalize_image_description(text) == text

    def test_empty_string_returns_empty(self):
        assert normalize_image_description("") == ""

    def test_whitespace_only_returns_empty(self):
        assert normalize_image_description("  \n\t  ") == ""

    def test_custom_limit_applies(self):
        result = normalize_image_description("x" * 20, limit=10)
        assert len(result) == 10
        assert result == "x" * 9 + "…"

    def test_custom_limit_exact_length_untouched(self):
        assert normalize_image_description("y" * 10, limit=10) == "y" * 10

    def test_collapse_happens_before_length_check(self):
        # 100 chars of payload plus interleaved extra whitespace: after
        # collapsing, the result is exactly at the limit and must survive.
        text = "  ".join(["cd"] * 50)  # collapses to "cd cd ... cd" (149 chars)
        collapsed = " ".join(["cd"] * 50)
        result = normalize_image_description(text, limit=len(collapsed))
        assert result == collapsed


class TestFormatImageNote:
    def test_description_renders_labelled_note(self):
        assert format_image_note("一只猫") == "[图片: 一只猫]"

    def test_empty_description_renders_bare_placeholder(self):
        assert format_image_note("") == "[图片]"

    def test_quoted_with_description(self):
        assert format_image_note("一只猫", quoted=True) == "[引用图片: 一只猫]"

    def test_quoted_empty_description(self):
        assert format_image_note("", quoted=True) == "[引用图片]"
