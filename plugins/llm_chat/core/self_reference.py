"""Resolve configured persona reference images for direct image-model input."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from utils.path import IMAGE_DIR

from .image_source import IMAGE_FETCH_MAX_BYTES, raw_to_image_data_url


def resolve_self_reference_image(
    relative_path: str | None,
    *,
    image_root: Path = IMAGE_DIR,
) -> Path | None:
    """Resolve one configured path strictly below the static image root."""

    if not isinstance(relative_path, str):
        return None
    normalized = relative_path.strip().replace("\\", "/")
    if not normalized:
        return None
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or not pure.parts or ":" in pure.parts[0]:
        return None
    if any(part in {"", ".", ".."} for part in pure.parts):
        return None

    try:
        root = image_root.resolve()
        candidate = root.joinpath(*pure.parts)
        if candidate.is_symlink():
            return None
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    return resolved if resolved.is_file() else None


def load_self_reference_image(
    relative_path: str | None,
    *,
    image_root: Path = IMAGE_DIR,
) -> tuple[bytes, str] | None:
    """Load one bounded, MIME-validated persona reference for an image API."""

    path = resolve_self_reference_image(relative_path, image_root=image_root)
    if path is None:
        return None
    try:
        with path.open("rb") as stream:
            data = stream.read(IMAGE_FETCH_MAX_BYTES + 1)
    except OSError:
        return None
    if not data or len(data) > IMAGE_FETCH_MAX_BYTES:
        return None
    data_url = raw_to_image_data_url(data[:256])
    if data_url is None:
        return None
    mime = data_url[5:].partition(";")[0].casefold()
    if mime not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
        return None
    return data, mime
