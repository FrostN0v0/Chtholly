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

_DEFAULT_RNG = random.Random()


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
