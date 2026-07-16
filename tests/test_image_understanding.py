"""Unit tests for inbound image description normalization and note formatting."""

from plugins.llm_chat.core.media import (
    format_image_note,
    normalize_image_tags,
    is_internal_media_record,
    normalize_image_description,
    strip_internal_media_records,
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


class TestNormalizeImageTags:
    def test_mixed_separators_prefixes_and_duplicates_preserve_order(self):
        raw = "1. happy; - cute，happy\n• sticker、3) cat；cute"

        assert normalize_image_tags(raw) == "happy，cute，sticker，cat"

    def test_limits_to_twenty_tags_and_uses_chinese_commas(self):
        raw = ";".join(f"{index}. tag-{index}" for index in range(1, 23))
        result = normalize_image_tags(raw)

        assert result.split("，") == [f"tag-{index}" for index in range(1, 21)]
        assert "," not in result

    def test_empty_input_returns_empty(self):
        assert normalize_image_tags(" \n ; ， 、 ") == ""


class TestFormatImageNote:
    def test_description_renders_labelled_note(self):
        assert format_image_note("一只猫") == "[图片: 一只猫]"

    def test_empty_description_renders_bare_placeholder(self):
        assert format_image_note("") == "[图片]"

    def test_quoted_with_description(self):
        assert format_image_note("一只猫", quoted=True) == "[引用图片: 一只猫]"

    def test_quoted_empty_description(self):
        assert format_image_note("", quoted=True) == "[引用图片]"


class TestInternalMediaRecords:
    def test_strips_false_sticker_record_from_model_reply(self):
        reply = "[发送了表情包: 纠结，挑选，认真看，可爱]只看立绘的话，我会选提丰。"

        assert strip_internal_media_records(reply) == "只看立绘的话，我会选提丰。"

    def test_strips_nested_tts_tags_through_outer_record(self):
        reply = "[用语音说: [softly] 晚安。[happy] 明天见。]\n文字补充"

        assert strip_internal_media_records(reply) == "文字补充"

    def test_strips_multiple_adjacent_records(self):
        reply = "[发送了表情包: 开心] [发送了语音: 早安]早上好。"

        assert strip_internal_media_records(reply) == "早上好。"

    def test_leaves_unclosed_record_unchanged(self):
        reply = "[发送了表情包: 不完整"

        assert strip_internal_media_records(reply) == reply

    def test_recognizes_only_standalone_records(self):
        assert is_internal_media_record("  [发送了语音: 晚安]  ")
        assert is_internal_media_record("[用语音说: [softly] 晚安。]")
        assert not is_internal_media_record("[发送了表情包: 开心]文字回复")
