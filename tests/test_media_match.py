"""Unit tests for dinggong filename parsing and media fuzzy matching."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from llm_chat_src.media import match_audio, match_image, parse_audio_text  # noqa: E402


class TestParseAudioText:
    def test_standard_format(self):
        assert parse_audio_text("11_你这个笨蛋！_.mp3") == "你这个笨蛋！"

    def test_no_trailing_underscore(self):
        assert parse_audio_text("19_ hi~_.mp3") == "hi~"

    def test_leading_space_after_number(self):
        assert (
            parse_audio_text("30_ 真是的，不管啦笨蛋！！你让人家怎么说嘛！！_.mp3")
            == "真是的，不管啦笨蛋！！你让人家怎么说嘛！！"
        )

    def test_unparseable_returns_none(self):
        assert parse_audio_text("random.mp3") is None

    def test_empty_text_returns_none(self):
        assert parse_audio_text("42__.mp3") is None


class TestMatchAudio:
    FILES = [
        Path("11_你这个笨蛋！_.mp3"),
        Path("13_我才没有为你担心！_.mp3"),
        Path("50_才没有吃醋呢，你在说什么傻话啊！_.mp3"),
        Path("random.mp3"),
    ]

    def test_close_match_wins(self):
        result = match_audio("你这个笨蛋", self.FILES)
        assert result is not None
        assert result.name == "11_你这个笨蛋！_.mp3"

    def test_below_threshold_returns_none(self):
        assert match_audio("完全不相关的天气预报内容播报", self.FILES) is None

    def test_empty_context_returns_none(self):
        assert match_audio("  ", self.FILES) is None

    def test_unparseable_files_excluded(self):
        assert match_audio("random", [Path("random.mp3")]) is None


class TestMatchImage:
    TAGGED = [
        ("fox_img/1.jpg", "狐狸,可爱,橙色,动物,毛茸茸"),
        ("fox_img/2.jpg", "雪地,狐狸,白色,冬天,安静"),
        ("cat/3.jpg", "猫,黑色,慵懒,沙发,睡觉"),
    ]

    def test_most_tag_hits_wins(self):
        assert match_image("想看雪地里的白色狐狸", self.TAGGED) == "fox_img/2.jpg"

    def test_single_hit(self):
        assert match_image("来只猫", self.TAGGED) == "cat/3.jpg"

    def test_no_hits_returns_none(self):
        assert match_image("汽车飞机大炮", self.TAGGED) is None

    def test_empty_context_returns_none(self):
        assert match_image("", self.TAGGED) is None
