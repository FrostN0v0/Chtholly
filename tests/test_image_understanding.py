"""Unit tests for inbound image description normalization and note formatting."""

import json

from plugins.llm_chat.core.media import (
    RECENT_MEME_HISTORY_NOTE,
    format_image_note,
    has_meaningful_text,
    normalize_image_tags,
    is_internal_media_record,
    sanitize_assistant_history,
    normalize_image_description,
    strip_internal_media_records,
)
from plugins.llm_chat.core.image_tag_metadata import (
    image_tag_format,
    image_tag_search_text,
    image_tag_avoids_context,
    image_tag_embedding_text,
    parse_image_tag_metadata,
    image_tag_catalog_summary,
    normalize_generated_image_tags,
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

    def test_punctuation_only_description_returns_empty(self):
        assert normalize_image_description(" . 。 …… ") == ""

    def test_emoji_only_description_remains_visible(self):
        assert normalize_image_description("🤨") == "🤨"

    def test_meaningful_text_accepts_words_numbers_and_symbols(self):
        assert has_meaningful_text("猫")
        assert has_meaningful_text("42")
        assert has_meaningful_text("🤨")
        assert not has_meaningful_text(" .。…… ")

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


class TestStructuredImageTagMetadata:
    def test_preserves_visible_text_and_separates_positive_and_avoid_semantics(self):
        raw = json.dumps(
            {
                "text": "握草！你怎么这么坏",
                "meaning": "惊讶又嗔怪地责怪对方使坏",
                "use_when": ["朋友恶作剧后吐槽", "被对方调侃时反击"],
                "avoid_when": ["早上好", "普通问候"],
                "tags": ["惊讶，嗔怪", "文字表情包", "兔耳少女"],
            },
            ensure_ascii=False,
        )

        normalized = normalize_generated_image_tags(raw)
        metadata = parse_image_tag_metadata(normalized)

        assert metadata is not None
        assert metadata.text == "握草！你怎么这么坏"
        assert metadata.tags == ("惊讶", "嗔怪", "文字表情包", "兔耳少女")
        assert "早上好" not in image_tag_embedding_text(normalized)
        assert "普通问候" not in image_tag_search_text(normalized)
        assert "避用：早上好、普通问候" in image_tag_catalog_summary(normalized)
        assert image_tag_avoids_context(normalized, "早上好呀")
        assert not image_tag_avoids_context(normalized, "你怎么这么坏")
        assert image_tag_format(normalized) == "structured"

    def test_visible_text_keeps_meaningful_quote_characters(self):
        normalized = normalize_generated_image_tags(
            json.dumps(
                {
                    "text": "“真的吗？”",
                    "meaning": "质疑",
                    "use_when": [],
                    "avoid_when": [],
                    "tags": ["文字表情包"],
                },
                ensure_ascii=False,
            )
        )

        metadata = parse_image_tag_metadata(normalized)
        assert metadata is not None
        assert metadata.text == "“真的吗？”"

    def test_plain_legacy_tags_are_wrapped_in_canonical_json(self):
        normalized = normalize_generated_image_tags("爆笑，社死，慌张")

        assert json.loads(normalized) == {
            "text": "",
            "meaning": "",
            "use_when": [],
            "avoid_when": [],
            "tags": ["爆笑", "社死", "慌张"],
        }
        assert image_tag_format("爆笑，社死，慌张") == "legacy"

    def test_malformed_json_is_rejected_instead_of_fragmented_into_tags(self):
        assert normalize_generated_image_tags('{"text":"broken"') == ""


class TestFormatImageNote:
    def test_description_renders_labelled_note(self):
        assert format_image_note("一只猫") == "[图片: 一只猫]"

    def test_empty_description_renders_bare_placeholder(self):
        assert format_image_note("") == "[图片]"

    def test_quoted_with_description(self):
        assert format_image_note("一只猫", quoted=True) == "[引用图片: 一只猫]"

    def test_quoted_empty_description(self):
        assert format_image_note("", quoted=True) == "[引用图片]"

    def test_quoted_source_role_is_explicit(self):
        assert format_image_note("一只猫", quoted=True, quoted_role="assistant") == "[引用自当前 Bot 的图片: 一只猫]"
        assert format_image_note("", quoted=True, quoted_role="participant") == "[引用自其他成员的图片]"
        assert format_image_note("", quoted=True, quoted_role="unknown") == "[引用自来源未知消息的图片]"


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

    def test_legacy_collection_record_remains_hidden_from_model_history(self):
        record = '[收藏了表情包:{"path":"memes/64.jpg","tags":"reaction,happy"}]'

        assert is_internal_media_record(record)
        sanitized = sanitize_assistant_history(record)
        assert sanitized is not None
        assert sanitized == RECENT_MEME_HISTORY_NOTE
        assert "memes/64.jpg" not in sanitized
        assert strip_internal_media_records(f"{record}Visible reply") == "Visible reply"
