"""Pure media matching: image tag search + dinggong audio filename matching."""

import re
from difflib import SequenceMatcher
from pathlib import Path

AUDIO_MATCH_THRESHOLD = 0.4

_FILENAME_RE = re.compile(r"^\s*\d+\s*_(?P<text>.+?)_?\s*$")


def parse_audio_text(filename: str) -> str | None:
    """Extract the spoken text from a `NN_<text>_.mp3` style filename.

    Returns None when the filename does not follow the pattern.
    """
    stem = Path(filename).stem
    match = _FILENAME_RE.match(stem)
    if not match:
        return None
    text = match.group("text").strip().strip("_").strip()
    return text or None


def match_audio(context: str, files: list[Path]) -> Path | None:
    """Fuzzy-match `context` against parsed clip texts; best above threshold wins."""
    if not context.strip():
        return None
    best: tuple[float, Path] | None = None
    for file in files:
        text = parse_audio_text(file.name)
        if text is None:
            continue
        score = SequenceMatcher(None, context, text).ratio()
        if best is None or score > best[0]:
            best = (score, file)
    if best is None or best[0] < AUDIO_MATCH_THRESHOLD:
        return None
    return best[1]


def match_image(context: str, tagged: list[tuple[str, str]]) -> str | None:
    """Keyword-overlap match: `tagged` is [(file_path, "tag1,tag2,...")].

    Returns the file path with the highest tag-hit count, or None when no
    tag appears in the context.
    """
    if not context.strip():
        return None
    best_path: str | None = None
    best_hits = 0
    for path, tag_str in tagged:
        tags = [t.strip() for t in tag_str.split(",") if t.strip()]
        hits = sum(1 for tag in tags if tag and tag in context)
        if hits > best_hits:
            best_hits = hits
            best_path = path
    return best_path
