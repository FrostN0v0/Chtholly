"""Safe normalization of native provider-generated images."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping, Sequence

from agno.media import Image as AgnoImage
from arclet.entari import Image as EntariImage

from ..web.policy import WebAccessError, normalize_public_url
from .image_source import IMAGE_FETCH_MAX_BYTES, raw_to_image_data_url

_ALLOWED_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})
_MAX_INLINE_SOURCE_CHARS = ((IMAGE_FETCH_MAX_BYTES + 2) // 3) * 4 + 256
_BASE64_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")
_MISSING = object()


def _read(value: object, key: str) -> object:
    if isinstance(value, Mapping):
        return value.get(key, _MISSING)
    return getattr(value, key, _MISSING)


def _image_from_bytes(data: bytes) -> AgnoImage | None:
    if not data or len(data) > IMAGE_FETCH_MAX_BYTES:
        return None
    try:
        data_url = raw_to_image_data_url(data)
    except Exception:
        return None
    if data_url is None:
        return None
    mime = data_url[5:].partition(";")[0].casefold()
    if mime not in _ALLOWED_MIME_TYPES:
        return None
    return AgnoImage(
        content=bytes(data),
        mime_type=mime,
        format=mime.removeprefix("image/"),
    )


def _decode_base64(payload: str) -> AgnoImage | None:
    compact = "".join(payload.split())
    if not compact or len(compact) > _MAX_INLINE_SOURCE_CHARS:
        return None
    if any(character not in _BASE64_CHARS for character in compact):
        return None
    try:
        data = base64.b64decode(compact, validate=True)
    except (ValueError, binascii.Error):
        return None
    return _image_from_bytes(data)


def _normalize_string(source: str) -> AgnoImage | None:
    candidate = source.strip()
    if not candidate or len(candidate) > max(_MAX_INLINE_SOURCE_CHARS, 2048):
        return None

    lowered = candidate.casefold()
    if lowered.startswith(("http://", "https://")):
        try:
            url = normalize_public_url(candidate)
        except WebAccessError:
            return None
        return AgnoImage(url=url)

    if lowered.startswith("data:"):
        header, separator, payload = candidate.partition(",")
        if not separator:
            return None
        header_parts = header.casefold().split(";")
        if not header_parts or not header_parts[0].startswith("data:image/") or "base64" not in header_parts[1:]:
            return None
        return _decode_base64(payload)

    if lowered.startswith("base64://"):
        return _decode_base64(candidate[9:])

    # Some OpenAI-compatible providers omit the data/base64 scheme. Accept only
    # strict base64-looking values and still require a valid sniffed image.
    compact = "".join(candidate.split())
    if (
        compact
        and len(compact) <= _MAX_INLINE_SOURCE_CHARS
        and all(character in _BASE64_CHARS for character in compact)
    ):
        return _decode_base64(compact)
    return None


def _normalize_object(source: object) -> AgnoImage | None:
    nested = _read(source, "image_url")
    if nested is not _MISSING and nested is not None:
        if isinstance(nested, str):
            return _normalize_string(nested)
        return _normalize_object(nested)
    # Local paths are intentionally never resolved for model output.
    filepath = _read(source, "filepath")
    if filepath is not _MISSING and filepath is not None:
        return None

    content = _read(source, "content")
    if content is not _MISSING and content is not None:
        if isinstance(content, bytes):
            return _image_from_bytes(content)
        if isinstance(content, (bytearray, memoryview)):
            return _image_from_bytes(bytes(content))
        if isinstance(content, str):
            return _normalize_string(content)
        return None

    url = _read(source, "url")
    if url is not _MISSING and isinstance(url, str):
        return _normalize_string(url)
    return None


def normalize_native_image(source: object) -> AgnoImage | None:
    """Normalize one provider image into a safe Agno image object."""

    if isinstance(source, AgnoImage):
        return _normalize_object(source)
    if isinstance(source, bytes):
        return _image_from_bytes(source)
    if isinstance(source, (bytearray, memoryview)):
        return _image_from_bytes(bytes(source))
    if isinstance(source, str):
        return _normalize_string(source)
    if isinstance(source, Mapping) or source is not None:
        return _normalize_object(source)
    return None


def _iter_values(value: object) -> Sequence[object]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _provider_message_images(response: object) -> Sequence[object]:
    choices = _read(response, "choices")
    choices_values = _iter_values(choices)
    if not choices_values:
        return ()
    message = _read(choices_values[0], "message")
    images = _read(message, "images")
    return _iter_values(images)


def extract_native_images(response: object) -> tuple[AgnoImage, ...]:
    """Extract safe images from Agno run output or a direct LiteLLM response."""

    cached = _read(response, "_llm_chat_native_images")
    if isinstance(cached, tuple) and all(isinstance(image, AgnoImage) for image in cached):
        return cached

    candidates: list[object] = []
    run_output = _read(response, "_run_output")
    if run_output is not _MISSING and run_output is not None:
        candidates.extend(_iter_values(_read(run_output, "images")))
    candidates.extend(_iter_values(_read(response, "images")))
    candidates.extend(_provider_message_images(response))

    normalized: list[AgnoImage] = []
    for candidate in candidates:
        image = normalize_native_image(candidate)
        if image is not None:
            normalized.append(image)
    result = tuple(normalized)
    try:
        setattr(response, "_llm_chat_native_images", result)
    except Exception:
        pass
    return result


def to_entari_image(image: AgnoImage) -> EntariImage:
    """Convert one already-normalized Agno image for Entari delivery."""

    if image.content is not None:
        return EntariImage.of(raw=image.content)
    if image.url is not None:
        return EntariImage.of(url=image.url)
    raise ValueError("Native image has no deliverable content")


__all__ = [
    "extract_native_images",
    "normalize_native_image",
    "to_entari_image",
]
