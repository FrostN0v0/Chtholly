"""Poke plugin resource loading contracts."""

import sys
from pathlib import Path
import subprocess


def test_poke_media_pools_include_classified_audio_and_memes(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    config_path = tmp_path / "entari.yml"
    soundboard_dir = tmp_path / "soundboard"
    meme_dir = tmp_path / "memes"
    dinggong_dir = tmp_path / "dinggong"
    shenying_dir = tmp_path / "shenying"
    dinggong_dir.mkdir()
    shenying_dir.mkdir()
    (dinggong_dir / "first.mp3").write_bytes(b"audio")
    (shenying_dir / "second.wav").write_bytes(b"audio")
    (soundboard_dir / "voice" / "person").mkdir(parents=True)
    (soundboard_dir / "root.mp3").write_bytes(b"audio")
    (soundboard_dir / "voice" / "person" / "nested.wav").write_bytes(b"audio")
    meme_dir.mkdir()
    (meme_dir / "reaction.jpg").write_bytes(b"image")
    (meme_dir / "video.mp4").write_bytes(b"video")
    config_path.write_text(
        f'basic:\n  external_dirs: ["{(root / "plugins").as_posix()}"]\nplugins: {{}}\n',
        encoding="utf-8",
    )
    script = f"""
import random
import sys
from pathlib import Path

root = Path({str(root)!r})
sys.path.insert(0, str(root))
sys.path.insert(0, str(root / "plugins"))

from arclet.entari.config import EntariConfig
from arclet.entari.plugin import load_plugin

EntariConfig.load(Path({str(config_path)!r}))
plugin = load_plugin("poke", config={{}})
assert plugin is not None
module = plugin.module
module.SOUNDBOARD_DIR = Path({str(soundboard_dir)!r})
module.MEME_DIR = Path({str(meme_dir)!r})
module.POKE_AUDIO_DIRS = (
    ("dinggong", Path({str(dinggong_dir)!r})),
    ("shenying", Path({str(shenying_dir)!r})),
)
module._audio_category_list = None
module._image_list = None
categories = module._audio_categories()
category_names = {{category.name for category in categories}}
assert {{"dinggong", "shenying", "soundboard/misc", "soundboard/voice/person"}} <= category_names
assert all(category.files for category in categories)
images = module._images()
assert [path.name for path in images] == ["reaction.jpg"]
assert module.POKE_TEXT_PERCENT == 10
assert module.POKE_IMAGE_PERCENT == 31
assert module.POKE_AUDIO_PERCENT == 45
rng = random.Random(0)
sampled_categories = {{rng.choice(categories).name for _ in range(500)}}
assert category_names <= sampled_categories
print("ok")
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout.strip().endswith("ok")
