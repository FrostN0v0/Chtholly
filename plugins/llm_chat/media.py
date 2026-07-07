"""Pure media matching: image tag search + dinggong audio filename matching."""

import re
from difflib import SequenceMatcher
from pathlib import Path

AUDIO_MATCH_THRESHOLD = 0.4

_FILENAME_RE = re.compile(r"^\s*\d+\s*_(?P<text>.+?)_?\s*$")
_TAG_SPLIT_RE = re.compile(r"[，,、;；\n\r]+")
_TAG_PREFIX_RE = re.compile(r"^\s*(?:[-*•]+|\d+[.)、])\s*")
_AUDIO_NORMALIZE_RE = re.compile(r"[\W_\s]+")
_LOW_SIGNAL_CHARS = frozenset("的了呢啊吗吧呀哦嗯哈我你他她它是不在有和跟给就都也还才又很真这那一个")
_AUDIO_SYNONYM_GROUPS = (
    ("早上问好", "早安", "早上好", "您早上好", "问早"),
)


def normalize_image_tags(text: str, *, limit: int = 20) -> str:
    """Return a compact comma-separated image tag line."""
    tags: list[str] = []
    seen: set[str] = set()
    for raw in _TAG_SPLIT_RE.split(text):
        tag = _TAG_PREFIX_RE.sub("", raw).strip().strip("`*_ \t\"'“”‘’。.!！")
        if not tag or tag in seen:
            continue
        seen.add(tag)
        tags.append(tag)
        if len(tags) >= limit:
            break
    return "，".join(tags)


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


def _normalize_audio_text(text: str) -> str:
    return _AUDIO_NORMALIZE_RE.sub("", text).lower()


def _audio_terms(text: str) -> set[str]:
    normalized = _normalize_audio_text(text)
    terms = {normalized} if normalized else set()
    for group in _AUDIO_SYNONYM_GROUPS:
        normalized_group = {_normalize_audio_text(term) for term in group}
        if any(term and term in normalized for term in normalized_group):
            terms.update(term for term in normalized_group if term)
    return terms


def _substring_score(context: str, text: str) -> float:
    if not context or not text:
        return 0.0
    shorter = min(context, text, key=len)
    if len(shorter) < 2:
        return 0.0
    if context in text or text in context:
        return min(1.0, 0.65 + len(shorter) * 0.03)
    return 0.0


def _char_overlap_score(context: str, text: str) -> float:
    context_chars = {char for char in context if char not in _LOW_SIGNAL_CHARS}
    if not context_chars:
        return 0.0
    text_chars = {char for char in text if char not in _LOW_SIGNAL_CHARS}
    return len(context_chars & text_chars) / len(context_chars)


def _audio_match_score(context: str, text: str) -> float:
    context_norm = _normalize_audio_text(context)
    text_norm = _normalize_audio_text(text)
    synonym_score = 0.9 if _audio_terms(context_norm) & _audio_terms(text_norm) else 0.0
    return max(
        _substring_score(context_norm, text_norm),
        _char_overlap_score(context_norm, text_norm),
        SequenceMatcher(None, context_norm, text_norm).ratio(),
        synonym_score,
    )


def match_audio(context: str, files: list[Path]) -> Path | None:
    """Fuzzy-match `context` against parsed clip texts; best above threshold wins."""
    if not context.strip():
        return None
    best: tuple[float, Path] | None = None
    for file in files:
        text = parse_audio_text(file.name)
        if text is None:
            continue
        score = _audio_match_score(context, text)
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
        tags = [t.strip() for t in _TAG_SPLIT_RE.split(tag_str) if t.strip()]
        hits = sum(1 for tag in tags if tag and tag in context)
        if hits > best_hits:
            best_hits = hits
            best_path = path
    return best_path
