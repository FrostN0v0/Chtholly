"""generate_image LLM tool implementation."""

from __future__ import annotations

from typing import Any, Literal, Protocol, cast
import asyncio
from dataclasses import field, dataclass
from collections.abc import Mapping, Callable, Sequence, Awaitable

from arclet.entari import Session
from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from ._rendering import WarningSink, HistoryAppender, deliver_image_bytes
from ..core.types import JSONType
from ._registration import register_tool
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


class ImageGenerator(Protocol):
    def __call__(self, **kwargs: object) -> Awaitable[object]: ...


ModelResolver = Callable[[str], ImageModelConfig]


@dataclass(slots=True)
class ImageGenerationToolContext:
    """Runtime dependencies and fixed provider policy for generated images."""

    resolve_model: ModelResolver
    generate: ImageGenerator
    append_history: HistoryAppender
    warn: WarningSink
    timeout_seconds: float
    quality: ImageQuality
    output_format: ImageOutputFormat
    output_compression: int
    semaphore: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(1))


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
        raise DeliveryError("image generation returned no image")
    item = data[0]
    base64_payload = _read(item, "b64_json")
    if isinstance(base64_payload, str) and base64_payload.strip():
        return f"base64://{base64_payload}"
    url = _read(item, "url")
    if isinstance(url, str) and url.strip():
        return url
    raise DeliveryError("image generation returned an unsupported image source")


async def _generated_image_bytes(session: Session, response: object) -> bytes:
    image = normalize_native_image(_first_image_source(response))
    if image is None:
        raise DeliveryError("generated image is invalid or exceeds the delivery size limit")
    if image.content is not None:
        return bytes(image.content)
    if image.url is not None:
        data = await fetch_image_bytes(session, image.url)
        if data is not None:
            return data
    raise DeliveryError("generated image could not be downloaded safely")


def _provider_extra(config: ImageModelConfig) -> dict[str, object]:
    allowed = {"default_headers", "extra_headers", "organization"}
    return {key: value for key, value in config.extra.items() if key in allowed}


def register_generate_image(
    dispatcher: PluginDispatcher[JSONType],
    runtime: ImageGenerationToolContext,
) -> Subscriber[JSONType]:
    """Register provider-backed original image generation and delivery."""

    async def generate_image(
        session: Session,
        prompt: str,
        size: ImageSize = DEFAULT_IMAGE_SIZE,
    ) -> str:
        """Generate and send exactly one new image with the server-configured image model.

        Use this for an explicit request to draw, create, or generate original visual content, regardless of which
        conversation model is active. Write a complete visual prompt containing only details needed for the requested
        image. When the current character or a user-provided image is the subject, include the visible appearance and
        requested changes in the prompt. Do not include secrets, private profile data, internal identifiers, local
        paths, tool instructions, or unrelated conversation history. Use send_image for existing local reactions,
        send_external_image for an existing direct image URL, screenshot_web_page for webpage rendering, and the
        deterministic rendering tools for tables, reports, or code layouts.

        Args:
            prompt (str): Complete standalone prompt for one original image, at most 32000 characters.
            size (str): Output size: 1024x1024, 1536x1024, or 1024x1536.
        Returns:
            str: Confirmed delivery status without exposing provider data.
        """

        normalized_prompt = normalize_image_prompt(prompt)
        normalized_size = normalize_image_size(size)
        compression = normalize_output_compression(runtime.output_compression)
        try:
            model = runtime.resolve_model(session.channel.id)
            async with runtime.semaphore:
                response = await asyncio.wait_for(
                    runtime.generate(
                        model=model.name,
                        prompt=normalized_prompt,
                        api_key=model.api_key,
                        api_base=model.base_url,
                        timeout=runtime.timeout_seconds,
                        n=1,
                        size=normalized_size,
                        max_retries=0,
                        quality=runtime.quality,
                        output_format=runtime.output_format,
                        output_compression=compression,
                        **_provider_extra(model),
                    ),
                    timeout=runtime.timeout_seconds,
                )
            data = await _generated_image_bytes(session, response)
        except asyncio.CancelledError:
            raise
        except DeliveryError:
            raise
        except asyncio.TimeoutError:
            runtime.warn("generate_image failed: timeout")
            raise DeliveryError("image generation timed out") from None
        except Exception as exc:
            runtime.warn(f"generate_image failed: {type(exc).__name__}")
            raise DeliveryError("the configured image generation service is unavailable") from None

        return await deliver_image_bytes(
            session,
            data,
            append_history=runtime.append_history,
            warn=runtime.warn,
            tool_name="generate_image",
            success_message=(
                "Generated image sent successfully. Do not claim another image was sent or repeat the prompt in the "
                "final response; return [END_OF_RESPONSE] when no supplement is needed."
            ),
        )

    return register_tool(dispatcher, generate_image)


__all__ = [
    "DEFAULT_IMAGE_SIZE",
    "ImageGenerationToolContext",
    "ImageOutputFormat",
    "ImageQuality",
    "ImageSize",
    "MAX_IMAGE_PROMPT_CHARS",
    "normalize_image_prompt",
    "normalize_image_size",
    "normalize_output_compression",
    "register_generate_image",
]
