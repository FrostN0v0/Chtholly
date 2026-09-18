"""generate_image LLM tool implementation."""

from __future__ import annotations

import asyncio
from dataclasses import field, dataclass

from arclet.entari import Session
from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from ._rendering import WarningSink, prepare_image_bytes
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
from ..core.tool_trace import record_tool_evidence
from ..image_edit_refs import current_image_edit_references
from ..agent_attachments import store_agent_attachment


@dataclass(slots=True)
class ImageGenerationToolContext:
    """Runtime dependencies and fixed provider policy for generated images."""

    resolve_model: ModelResolver
    generate: ImageProvider
    edit: ImageProvider
    warn: WarningSink
    timeout_seconds: float
    quality: ImageQuality
    output_format: ImageOutputFormat
    output_compression: int
    semaphore: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(1))


_REFERENCE_GENERATION_INSTRUCTION = (
    "Use the provided input image as the configured persona identity reference for a new image. "
    "Preserve recognizable face, hair, clothing, headwear and accessories unless the requested prompt changes them. "
    "Create the new pose, expression, composition and background requested below; do not copy unrelated source text, "
    "logos, watermark or background unless requested. Produce exactly one finished image and no explanatory text "
    "inside the image unless requested.\n\nRequested image:\n"
)


def register_generate_image(
    dispatcher: PluginDispatcher[JSONType],
    runtime: ImageGenerationToolContext,
) -> Subscriber[JSONType]:
    """Register provider-backed original image generation and preparation."""

    async def generate_image(
        session: Session,
        prompt: str,
        size: ImageSize = DEFAULT_IMAGE_SIZE,
        use_persona_reference: bool = False,
    ) -> dict[str, JSONType]:
        """Generate a new image, optionally using the configured persona's actual reference pixels.

        For an image of yourself/current persona, set use_persona_reference=true when runtime_context reports a
        configured reference. The runtime uploads the original image to the dedicated image model. Do not redescribe
        it or supply a path. Leave false for unrelated subjects. Missing requested references fail without text-only
        fallback. Supplied user-image edits and real web-reference edits use edit_image instead. Prepare only; pass
        the returned media_ref to send_msg for delivery.

        Args:
            prompt: Visual instructions for the new image; no secrets, internal IDs, paths or unrelated history.
            size: Output size: 1024x1024, 1536x1024, or 1024x1536.
            use_persona_reference: Use the current persona's configured reference as actual image input.
        """
        edit_references = current_image_edit_references()
        if edit_references is not None and edit_references.requires_image_edit:
            if edit_references.requires_web_reference:
                raise DeliveryError(
                    "this turn requires a captured web reference; use capture_web_reference followed by edit_image"
                )
            raise DeliveryError("this turn requires editing the supplied image; use edit_image")
        normalized_prompt = normalize_image_prompt(prompt)
        normalized_size = normalize_image_size(size)
        compression = normalize_output_compression(runtime.output_compression)
        try:
            model = runtime.resolve_model(session.channel.id)
            persona_reference = None
            if use_persona_reference:
                if edit_references is None:
                    raise DeliveryError("persona references require an active llm_chat generation")
                persona_reference = edit_references.resolve_persona_reference()
            request: dict[str, object] = {
                "model": model.name,
                "prompt": normalized_prompt,
                "api_key": model.api_key,
                "api_base": model.base_url,
                "timeout": runtime.timeout_seconds,
                "n": 1,
                "size": normalized_size,
                "quality": runtime.quality,
                "max_retries": 0,
                **image_provider_extra(model),
            }
            provider = runtime.generate
            if persona_reference is None:
                request.update(output_format=runtime.output_format, output_compression=compression)
            else:
                provider = runtime.edit
                request.update(
                    prompt=f"{_REFERENCE_GENERATION_INSTRUCTION}{normalized_prompt}",
                    image=[persona_reference.data],
                    input_fidelity="high",
                    response_format="b64_json",
                )
                record_tool_evidence({"attachments": [persona_reference.attachment], "reference_count": 1})
            async with runtime.semaphore:
                response = await asyncio.wait_for(provider(**request), timeout=runtime.timeout_seconds)
            data = await image_response_bytes(session, response)
            if persona_reference is not None:
                output_attachment = store_agent_attachment(
                    data,
                    kind="output",
                    source="image_generation",
                    index=1,
                    label="Prepared reference-conditioned image",
                    root=edit_references.attachment_root if edit_references is not None else None,
                )
                record_tool_evidence({"attachments": [output_attachment]})
        except asyncio.CancelledError:
            raise
        except DeliveryError:
            raise
        except ValueError as exc:
            raise DeliveryError(str(exc)) from None
        except asyncio.TimeoutError:
            runtime.warn("generate_image failed: timeout")
            raise DeliveryError("image generation timed out") from None
        except Exception as exc:
            runtime.warn(f"generate_image failed: {type(exc).__name__}")
            raise DeliveryError("the configured image generation service is unavailable") from None

        return await prepare_image_bytes(
            session,
            data,
            warn=runtime.warn,
            tool_name="generate_image",
        )

    return register_tool(dispatcher, generate_image)


__all__ = ["ImageGenerationToolContext", "register_generate_image"]
