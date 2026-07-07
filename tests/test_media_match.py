"""Unit tests for dinggong filename parsing and media fuzzy matching."""

import sys
import random
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from llm_chat_src.media import (  # noqa: E402
    match_audio,
    match_image,
    parse_audio_text,
    is_random_request,
    rank_images_by_tags,
)


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
        Path("05_什么“早安”，应该说“您早上好”才对！_.mp3"),
        Path("random.mp3"),
    ]

    def test_close_match_wins(self):
        result = match_audio("你这个笨蛋", self.FILES)
        assert result is not None
        assert result.name == "11_你这个笨蛋！_.mp3"

    def test_morning_intent_match_wins(self):
        result = match_audio("早上问好", self.FILES)
        assert result is not None
        assert result.name == "05_什么“早安”，应该说“您早上好”才对！_.mp3"

    def test_morning_sentence_match_wins(self):
        result = match_audio("发一段早上问好的语音", self.FILES)
        assert result is not None
        assert result.name == "05_什么“早安”，应该说“您早上好”才对！_.mp3"

    def test_exact_old_match_still_passes(self):
        result = match_audio("我才没有为你担心", self.FILES)
        assert result is not None
        assert result.name == "13_我才没有为你担心！_.mp3"

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

    def test_chinese_comma_tags_match(self):
        tagged = [("morning/1.jpg", "早安，开心，可爱")]
        assert match_image("发一张开心早安的可爱表情图", tagged) == "morning/1.jpg"

    def test_no_hits_returns_none(self):
        assert match_image("汽车飞机大炮", self.TAGGED) is None

    def test_empty_context_returns_none(self):
        assert match_image("", self.TAGGED) is None


class TestIsRandomRequest:
    def test_casual_random_phrases_match(self):
        assert is_random_request("随便发一张")
        assert is_random_request("随意")
        assert is_random_request("任意挑一个")
        assert is_random_request("都行")
        assert is_random_request("RANDOM pick")

    def test_specific_requests_do_not_match(self):
        assert not is_random_request("生气的图")
        assert not is_random_request("")


class TestRankImagesByTags:
    # Regression for the real-world 104/136 bug: generic style tags shared by
    # many images must not outweigh one rare discriminative emotion tag.
    POOL = [
        ("filler1.jpg", "动漫,兽耳,可爱"),
        ("filler2.jpg", "动漫,兽耳,可爱"),
        ("filler3.jpg", "动漫,兽耳,可爱"),
        ("104.jpg", "动漫,兽耳,少女,绿色裙子,可爱"),
        ("136.jpg", "猫咪，狐耳少女，对峙，漫画风，生气"),
    ]

    def test_rare_emotion_tag_beats_generic_style_tags(self):
        ranked = rank_images_by_tags("生气 可爱 兽耳", self.POOL)
        assert ranked
        assert ranked[0][0] == "136.jpg"
        paths = [path for path, _score in ranked]
        assert paths.index("136.jpg") < paths.index("104.jpg")

    def test_zero_hit_images_are_dropped(self):
        ranked = rank_images_by_tags("生气", self.POOL)
        assert [path for path, _score in ranked] == ["136.jpg"]


class TestMatchImageTies:
    POOL = [
        ("a.jpg", "生气"),
        ("b.jpg", "生气"),
        ("filler.jpg", "动漫"),
    ]

    def test_seeded_rng_is_deterministic_within_tie_set(self):
        first = match_image("生气", self.POOL, rng=random.Random(0))
        second = match_image("生气", self.POOL, rng=random.Random(0))
        assert first in {"a.jpg", "b.jpg"}
        assert first == second

    def test_all_seeds_stay_within_tie_set(self):
        picks = {match_image("生气", self.POOL, rng=random.Random(seed)) for seed in range(10)}
        assert picks <= {"a.jpg", "b.jpg"}


class TestMatchAudioWindow:
    FILES = [
        Path("01_你这个笨蛋！_.mp3"),
        Path("02_笨蛋笨蛋！_.mp3"),
        Path("03_今天天气如何？_.mp3"),
    ]

    def test_window_random_pick_stays_in_near_best_set(self):
        expected = {"01_你这个笨蛋！_.mp3", "02_笨蛋笨蛋！_.mp3"}
        for seed in range(10):
            picked = match_audio("笨蛋", self.FILES, rng=random.Random(seed))
            assert picked is not None
            assert picked.name in expected

    def test_below_threshold_still_returns_none(self):
        assert match_audio("完全无关的深夜播报内容", self.FILES) is None
