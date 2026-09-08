"""generate_image LLM tool implementation."""

from __future__ import annotations

import asyncio
from dataclasses import field, dataclass

from arclet.entari import Session
from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from ._rendering import WarningSink, HistoryAppender, deliver_image_bytes
from ..core.types import JSONType
from ._registration import register_tool
from ..core.delivery import DeliveryError
from ._image_provider import (
    DEFAULT_IMAGE_SIZE,
    ImageSize,
    ImageQuality,
    ImageProvider,
    ModelResolver,
    ImageOutputFormat,
    image_provider_extra,
    image_response_bytes,
    normalize_image_size,
    normalize_image_prompt,
    normalize_output_compression,
)
from ..image_edit_refs import current_image_edit_references


@dataclass(slots=True)
class ImageGenerationToolContext:
    """Runtime dependencies and fixed provider policy for generated images."""

    resolve_model: ModelResolver
    generate: ImageProvider
    append_history: HistoryAppender
    warn: WarningSink
    timeout_seconds: float
    quality: ImageQuality
    output_format: ImageOutputFormat
    output_compression: int
    semaphore: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(1))


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

        Use this only for original visual content that does not require copying a real person or character from a web
        reference. When the current turn requires a web visual reference, call capture_web_reference and then
        edit_image instead; this tool is rejected at runtime for that turn. Write a complete visual prompt containing
        only details needed for the requested image. Do not include secrets, private profile data, internal identifiers,
        local paths, tool instructions, or unrelated conversation history. Use send_image for existing local reactions,
        send_external_image for an existing direct image URL, screenshot_web_page for webpage rendering, and the
        deterministic rendering tools for tables, reports, or code layouts.

        Args:
            prompt (str): Complete standalone prompt for one original image, at most 32000 characters.
            size (str): Output size: 1024x1024, 1536x1024, or 1024x1536.
        Returns:
            str: Confirmed delivery status without exposing provider data.
        """

        edit_references = current_image_edit_references()
        if edit_references is not None and edit_references.requires_web_reference:
            raise DeliveryError(
                "this turn requires a captured web reference; use capture_web_reference followed by edit_image"
            )
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
                        **image_provider_extra(model),
                    ),
                    timeout=runtime.timeout_seconds,
                )
            data = await image_response_bytes(session, response)
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


__all__ = ["ImageGenerationToolContext", "register_generate_image"]
