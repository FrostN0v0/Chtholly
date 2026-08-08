"""Pure media matching: image tag search + dinggong audio filename matching."""

import re
import math
import random
from difflib import SequenceMatcher
from pathlib import Path
from collections import Counter

AUDIO_MATCH_THRESHOLD = 0.4
AUDIO_NEAR_WINDOW = 0.05

_FILENAME_RE = re.compile(r"^\s*\d+\s*_(?P<text>.+?)_?\s*$")
_TAG_SPLIT_RE = re.compile(r"[，,、;；\n\r]+")
_TAG_PREFIX_RE = re.compile(r"^\s*(?:[-*•]+|\d+[.)、])\s*")
_AUDIO_NORMALIZE_RE = re.compile(r"[\W_\s]+")
_LOW_SIGNAL_CHARS = frozenset("的了呢啊吗吧呀哦嗯哈我你他她它是不在有和跟给就都也还才又很真这那一个")
_AUDIO_SYNONYM_GROUPS = (("早上问好", "早安", "早上好", "您早上好", "问早"),)
_RANDOM_REQUEST_RE = re.compile(r"随便|随意|任意|都行|都可以|random", re.IGNORECASE)
_TTS_CONTROL_TAG_RE = re.compile(r"\[[^\[\]\r\n]{1,40}\]\s*")
_STICKER_RECORD_PREFIX = "[发送了表情包:"
_AUDIO_RECORD_PREFIX = "[发送了语音:"
_TTS_RECORD_PREFIX = "[用语音说:"
_NATIVE_IMAGE_RECORD_PREFIX = "[发送了图片]"
MEME_COLLECTION_RECORD_PREFIX = "[收藏了表情包:"
RECENT_MEME_HISTORY_NOTE = "[最近成功收藏了一张表情包，可按用户要求重新发送]"
_INTERNAL_MEDIA_RECORD_PREFIXES = (
    _STICKER_RECORD_PREFIX,
    _AUDIO_RECORD_PREFIX,
    _TTS_RECORD_PREFIX,
    _NATIVE_IMAGE_RECORD_PREFIX,
    MEME_COLLECTION_RECORD_PREFIX,
)


_DEFAULT_RNG = random.Random()


def _find_record_end(text: str, start: int) -> int | None:
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "[":
            depth += 1
        elif text[index] == "]":
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def _find_internal_media_records(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    cursor = 0
    while cursor < len(text):
        starts = [start for prefix in _INTERNAL_MEDIA_RECORD_PREFIXES if (start := text.find(prefix, cursor)) >= 0]
        if not starts:
            break
        start = min(starts)
        end = _find_record_end(text, start)
        if end is None:
            cursor = start + 1
            continue
        spans.append((start, end))
        cursor = end
    return spans


def is_internal_media_record(text: str) -> bool:
    """Return whether text is exactly one reserved media history record."""
    stripped = text.strip()
    spans = _find_internal_media_records(stripped)
    return len(spans) == 1 and spans[0] == (0, len(stripped))


def strip_internal_media_records(text: str) -> str:
    """Remove reserved media history records from model-authored output."""
    spans = _find_internal_media_records(text)
    if not spans:
        return text
    parts: list[str] = []
    cursor = 0
    for start, end in spans:
        parts.append(text[cursor:start])
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts).strip()


def sanitize_assistant_history(text: str) -> str | None:
    """Render stored assistant history without exposing reserved media records."""
    stripped = text.strip()
    if not is_internal_media_record(stripped):
        return strip_internal_media_records(stripped) or None
    if stripped.startswith(MEME_COLLECTION_RECORD_PREFIX):
        return RECENT_MEME_HISTORY_NOTE
    if stripped.startswith(_STICKER_RECORD_PREFIX):
        return None
    for prefix in (_AUDIO_RECORD_PREFIX, _TTS_RECORD_PREFIX):
        if stripped.startswith(prefix):
            payload = stripped[len(prefix) : -1].strip()
            if prefix == _TTS_RECORD_PREFIX:
                payload = _TTS_CONTROL_TAG_RE.sub("", payload)
            normalized = " ".join(payload.split())
            return normalized or None
    return None


def is_random_request(text: str) -> bool:
    """Whether the request asks for an arbitrary pick instead of a match."""
    return bool(_RANDOM_REQUEST_RE.search(text))


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


def normalize_image_description(text: str, *, limit: int = 100) -> str:
    """Collapse whitespace/newlines and cap length for prompt injection."""
    collapsed = " ".join(text.split())
    if len(collapsed) > limit:
        collapsed = collapsed[: limit - 1] + "…"
    return collapsed


def format_image_note(description: str, *, quoted: bool = False) -> str:
    """Render the inline marker injected into chat content."""
    label = "引用图片" if quoted else "图片"
    return f"[{label}: {description}]" if description else f"[{label}]"


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


def match_audio(context: str, files: list[Path], *, rng: random.Random | None = None) -> Path | None:
    """Fuzzy-match `context` against parsed clip texts.

    Candidates within AUDIO_NEAR_WINDOW of the best score are picked randomly
    so identical requests can vary between near-equivalent clips.
    """
    if not context.strip():
        return None
    scored: list[tuple[float, Path]] = []
    for file in files:
        text = parse_audio_text(file.name)
        if text is None:
            continue
        scored.append((_audio_match_score(context, text), file))
    if not scored:
        return None
    best = max(score for score, _ in scored)
    if best < AUDIO_MATCH_THRESHOLD:
        return None
    window = [file for score, file in scored if score >= best - AUDIO_NEAR_WINDOW]
    return (rng or _DEFAULT_RNG).choice(window)


def rank_images_by_tags(context: str, tagged: list[tuple[str, str]]) -> list[tuple[str, float]]:
    """IDF-weighted tag-hit ranking: rare tags outweigh ubiquitous style tags.

    Each tag hit contributes log((N + 1) / df); a tag carried by nearly every
    image contributes almost nothing, so one discriminative hit (e.g. 生气)
    beats several generic hits (动漫/可爱). Zero-score entries are dropped.
    """
    if not context.strip() or not tagged:
        return []
    total = len(tagged)
    tag_sets: list[tuple[str, set[str]]] = []
    df: Counter[str] = Counter()
    for path, tag_str in tagged:
        tags = {t.strip() for t in _TAG_SPLIT_RE.split(tag_str) if t.strip()}
        tag_sets.append((path, tags))
        df.update(tags)
    scored: list[tuple[str, float]] = []
    for path, tags in tag_sets:
        score = sum(math.log((total + 1) / df[tag]) for tag in tags if tag in context)
        if score > 0.0:
            scored.append((path, score))
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored


def match_image(
    context: str,
    tagged: list[tuple[str, str]],
    *,
    rng: random.Random | None = None,
) -> str | None:
    """Best IDF-ranked image; ties at the top score are picked randomly."""
    ranked = rank_images_by_tags(context, tagged)
    if not ranked:
        return None
    best = ranked[0][1]
    top = [path for path, score in ranked if score >= best - 1e-9]
    return (rng or _DEFAULT_RNG).choice(top)
