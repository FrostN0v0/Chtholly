"""Import-safe poke media classification helpers."""

from __future__ import annotations

from stat import S_ISREG
from typing import Literal
from pathlib import Path
from dataclasses import dataclass
from collections.abc import Iterable

SUPPORTED_AUDIO_SUFFIXES = frozenset({".aac", ".m4a", ".mp3", ".wav"})
SUPPORTED_IMAGE_SUFFIXES = frozenset({".gif", ".jpeg", ".jpg", ".png", ".webp"})
MAX_POKE_AUDIO_BYTES = 1024 * 1024
MAX_POKE_IMAGE_BYTES = 6 * 1024 * 1024

PokeResponseKind = Literal["text", "image", "audio", "none"]


@dataclass(frozen=True, slots=True)
class AudioCategory:
    name: str
    files: tuple[Path, ...]


def _is_eligible_file(path: Path, suffixes: frozenset[str], max_file_size: int) -> bool:
    if path.suffix.lower() not in suffixes:
        return False
    try:
        file_stat = path.lstat()
    except OSError:
        return False
    return S_ISREG(file_stat.st_mode) and 0 < file_stat.st_size <= max_file_size


def collect_audio_categories(
    flat_directories: Iterable[tuple[str, Path]],
    classified_directory: Path,
    *,
    max_file_size: int = MAX_POKE_AUDIO_BYTES,
) -> tuple[AudioCategory, ...]:
    categories: dict[str, list[Path]] = {}

    for category_name, directory in flat_directories:
        if not directory.is_dir():
            continue
        files = sorted(
            path for path in directory.iterdir() if _is_eligible_file(path, SUPPORTED_AUDIO_SUFFIXES, max_file_size)
        )
        if files:
            categories[category_name] = files

    if classified_directory.is_dir():
        for path in sorted(classified_directory.rglob("*")):
            if not _is_eligible_file(path, SUPPORTED_AUDIO_SUFFIXES, max_file_size):
                continue
            relative_parent = path.relative_to(classified_directory).parent.as_posix()
            category_name = f"soundboard/{relative_parent if relative_parent != '.' else 'misc'}"
            categories.setdefault(category_name, []).append(path)

    return tuple(AudioCategory(name=name, files=tuple(files)) for name, files in sorted(categories.items()) if files)


def collect_image_files(
    directory: Path,
    *,
    max_file_size: int = MAX_POKE_IMAGE_BYTES,
) -> tuple[Path, ...]:
    if not directory.is_dir():
        return ()
    return tuple(
        sorted(path for path in directory.iterdir() if _is_eligible_file(path, SUPPORTED_IMAGE_SUFFIXES, max_file_size))
    )


def select_response_kind(
    roll: int,
    *,
    text_weight: int,
    image_weight: int,
    audio_weight: int,
) -> PokeResponseKind:
    if roll < text_weight:
        return "text"
    if roll < text_weight + image_weight:
        return "image"
    if roll < text_weight + image_weight + audio_weight:
        return "audio"
    return "none"


__all__ = [
    "MAX_POKE_AUDIO_BYTES",
    "MAX_POKE_IMAGE_BYTES",
    "SUPPORTED_AUDIO_SUFFIXES",
    "SUPPORTED_IMAGE_SUFFIXES",
    "AudioCategory",
    "PokeResponseKind",
    "collect_audio_categories",
    "collect_image_files",
    "select_response_kind",
]
