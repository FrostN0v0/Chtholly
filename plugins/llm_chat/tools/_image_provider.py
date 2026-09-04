"""Shared validation and provider response decoding for image model tools."""

from __future__ import annotations

from typing import Any, Literal, Protocol, cast
from collections.abc import Mapping, Callable, Sequence, Awaitable

from arclet.entari import Session

from ..core.delivery import DeliveryError
from ..core.image_source import fetch_image_bytes
from ..core.native_images import normalize_native_image

ImageSize = Literal["1024x1024", "1536x1024", "1024x1536"]
ImageQuality = Literal["auto", "low", "medium", "high"]
ImageOutputFormat = Literal["png", "jpeg", "webp"]

MAX_IMAGE_PROMPT_CHARS = 32_000
DEFAULT_IMAGE_SIZE: ImageSize = "1024x1024"
_MISSING = object()


class ImageModelConfig(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def api_key(self) -> str | None: ...

    @property
    def base_url(self) -> str: ...

    @property
    def extra(self) -> Mapping[str, Any]: ...


class ImageProvider(Protocol):
    def __call__(self, **kwargs: object) -> Awaitable[object]: ...


ModelResolver = Callable[[str], ImageModelConfig]


def _read(value: object, key: str) -> object:
    if isinstance(value, Mapping):
        return value.get(key, _MISSING)
    return getattr(value, key, _MISSING)


def normalize_image_prompt(value: str) -> str:
    """Validate one provider prompt without logging or exposing its content."""

    if not isinstance(value, str):
        raise DeliveryError("prompt must be a string")
    normalized = value.strip()
    if not normalized:
        raise DeliveryError("prompt is required")
    if len(normalized) > MAX_IMAGE_PROMPT_CHARS:
        raise DeliveryError(f"prompt exceeds the configured character limit ({MAX_IMAGE_PROMPT_CHARS})")
    return normalized


def normalize_image_size(value: str) -> ImageSize:
    """Restrict generated images to the delivery-tested aspect ratios."""

    if value not in {"1024x1024", "1536x1024", "1024x1536"}:
        raise DeliveryError("size must be 1024x1024, 1536x1024, or 1024x1536")
    return cast(ImageSize, value)


def normalize_output_compression(value: int) -> int:
    """Validate provider compression as an integer percentage."""

    if type(value) is not int or not 0 <= value <= 100:
        raise DeliveryError("image output compression must be an integer between 0 and 100")
    return value


def _first_image_source(response: object) -> str:
    data = _read(response, "data")
    if not isinstance(data, Sequence) or isinstance(data, (str, bytes, bytearray)) or not data:
        raise DeliveryError("image provider returned no image")
    item = data[0]
    base64_payload = _read(item, "b64_json")
    if isinstance(base64_payload, str) and base64_payload.strip():
        return f"base64://{base64_payload}"
    url = _read(item, "url")
    if isinstance(url, str) and url.strip():
        return url
    raise DeliveryError("image provider returned an unsupported image source")


async def image_response_bytes(session: Session, response: object) -> bytes:
    """Decode one safe bounded image from a LiteLLM image response."""

    image = normalize_native_image(_first_image_source(response))
    if image is None:
        raise DeliveryError("image provider output is invalid or exceeds the delivery size limit")
    if image.content is not None:
        return bytes(image.content)
    if image.url is not None:
        data = await fetch_image_bytes(session, image.url)
        if data is not None:
            return data
    raise DeliveryError("image provider output could not be downloaded safely")


def image_provider_extra(config: ImageModelConfig) -> dict[str, object]:
    """Forward only provider transport metadata, never arbitrary model extras."""

    allowed = {"default_headers", "extra_headers", "organization"}
    return {key: value for key, value in config.extra.items() if key in allowed}


__all__ = [
    "DEFAULT_IMAGE_SIZE",
    "ImageModelConfig",
    "ImageOutputFormat",
    "ImageProvider",
    "ImageQuality",
    "ImageSize",
    "MAX_IMAGE_PROMPT_CHARS",
    "ModelResolver",
    "image_provider_extra",
    "image_response_bytes",
    "normalize_image_prompt",
    "normalize_image_size",
    "normalize_output_compression",
]
