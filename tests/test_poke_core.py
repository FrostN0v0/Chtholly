"""Pure poke media classification contracts."""

from pathlib import Path

from utils.poke_core import collect_image_files, select_response_kind, collect_audio_categories


def _write_file(path: Path, size: int = 1) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def test_collect_audio_categories_groups_leaf_directories_and_filters_files(tmp_path: Path) -> None:
    dinggong = tmp_path / "dinggong"
    shenying = tmp_path / "shenying"
    soundboard = tmp_path / "soundboard"

    _write_file(dinggong / "first.mp3")
    _write_file(shenying / "second.AAC")
    _write_file(soundboard / "root.wav")
    _write_file(soundboard / "voice" / "person" / "nested.m4a")
    _write_file(soundboard / "video.mp4")
    _write_file(soundboard / "oversized.mp3", size=5)
    _write_file(soundboard / "empty.mp3", size=0)

    categories = collect_audio_categories(
        (("dinggong", dinggong), ("shenying", shenying)),
        soundboard,
        max_file_size=4,
    )

    assert {category.name: tuple(path.name for path in category.files) for category in categories} == {
        "dinggong": ("first.mp3",),
        "shenying": ("second.AAC",),
        "soundboard/misc": ("root.wav",),
        "soundboard/voice/person": ("nested.m4a",),
    }


def test_collect_image_files_accepts_only_bounded_images(tmp_path: Path) -> None:
    memes = tmp_path / "memes"
    animation = _write_file(memes / "animation.GIF")
    reaction = _write_file(memes / "reaction.jpg")
    _write_file(memes / "video.mp4")
    _write_file(memes / "oversized.png", size=5)
    _write_file(memes / "empty.webp", size=0)

    assert collect_image_files(memes, max_file_size=4) == (animation, reaction)


def test_select_response_kind_uses_explicit_overall_probabilities() -> None:
    weights = {
        "text_weight": 10,
        "image_weight": 31,
        "audio_weight": 45,
    }

    assert select_response_kind(0, **weights) == "text"
    assert select_response_kind(9, **weights) == "text"
    assert select_response_kind(10, **weights) == "image"
    assert select_response_kind(40, **weights) == "image"
    assert select_response_kind(41, **weights) == "audio"
    assert select_response_kind(85, **weights) == "audio"
    assert select_response_kind(86, **weights) == "none"
    assert select_response_kind(99, **weights) == "none"
