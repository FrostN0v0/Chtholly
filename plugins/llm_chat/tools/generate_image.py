"""generate_image LLM tool implementation."""

from __future__ import annotations

from typing import cast
import asyncio
from dataclasses import field, dataclass

from arclet.entari import Session
from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from ._rendering import WarningSink, prepare_image_bytes
from ..core.types import JSONType
from ..image_inputs import ImageInputError, current_image_inputs
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
from ..agent_attachments import store_agent_attachment
from ..core.media_delivery import MAX_IMAGE_REFERENCES, ImageProvenance, current_media_requirements


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
    "Use the provided images as visual identity and appearance references for a new image. "
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
        reference_image_refs: list[str] = cast(list[str], None),
    ) -> dict[str, JSONType]:
        """Generate a new image, optionally using the configured persona's actual reference pixels.

        For an image of yourself/current persona, set use_persona_reference=true when runtime_context reports a
        configured reference. The runtime uploads the original image to the dedicated image model. Do not redescribe
        it or supply a path. Leave false for unrelated subjects. Missing requested references fail without text-only
        fallback. Use edit_image only when modifying a specific source image. For new creations based on real web
        images, pass matched refs from capture_web_reference in reference_image_refs; no source upload is needed.
        Prepare only; pass the returned media_ref to send_msg for delivery.

        Args:
            prompt: Visual instructions for the new image; no secrets, internal IDs, paths or unrelated history.
            size: Output size: 1024x1024, 1536x1024, or 1024x1536.
            use_persona_reference: Use the current persona's configured reference as actual image input.
            reference_image_refs: Zero to four authorized image_refs uploaded as real visual references.
        """
        inputs = current_image_inputs()
        requirements = current_media_requirements()
        if requirements is not None and requirements.intent.requires_source_edit:
            raise DeliveryError("this turn requires editing a specific source image; use edit_image")
        normalized_prompt = normalize_image_prompt(prompt)
        normalized_size = normalize_image_size(size)
        compression = normalize_output_compression(runtime.output_compression)
        try:
            model = runtime.resolve_model(session.channel.id)
            requested_refs = [] if reference_image_refs is None else reference_image_refs
            if not isinstance(requested_refs, list) or not all(isinstance(item, str) for item in requested_refs):
                raise DeliveryError("reference_image_refs must be a list of authorized image references")
            if len(requested_refs) > MAX_IMAGE_REFERENCES:
                raise DeliveryError(f"reference_image_refs exceeds the configured limit ({MAX_IMAGE_REFERENCES})")
            if (requested_refs or use_persona_reference) and inputs is None:
                raise DeliveryError("image references require an active llm_chat generation")
            references = []
            if inputs is not None:
                references = [await inputs.resolve(session, ref, purpose="reference") for ref in requested_refs]
                if use_persona_reference:
                    references.append(await inputs.resolve_persona(session))
            provenance = ImageProvenance(
                "generated",
                reference_image_refs=tuple(reference.image_ref for reference in references),
                reference_sources=tuple(reference.source for reference in references),
            )
            if requirements is not None and not requirements.accepts(provenance):
                raise DeliveryError("this turn requires at least one matched web reference from capture_web_reference")
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
            if not references:
                request.update(output_format=runtime.output_format, output_compression=compression)
            else:
                provider = runtime.edit
                request.update(
                    prompt=f"{_REFERENCE_GENERATION_INSTRUCTION}{normalized_prompt}",
                    image=[reference.data for reference in references],
                    input_fidelity="high",
                    response_format="b64_json",
                )
                record_tool_evidence(
                    {
                        "attachments": [
                            {
                                **inputs.audit_view(reference.image_ref),
                                "label": f"Visual reference {index} sent to image model",
                                "provider_index": index,
                            }
                            for index, reference in enumerate(references, start=1)
                        ]
                        if inputs is not None
                        else [],
                        "reference_count": len(references),
                    }
                )
            async with runtime.semaphore:
                response = await asyncio.wait_for(provider(**request), timeout=runtime.timeout_seconds)
            data = await image_response_bytes(session, response)
            if references:
                try:
                    output_attachment = store_agent_attachment(
                        data,
                        kind="output",
                        source="image_generation",
                        index=1,
                        label="Prepared reference-conditioned image",
                        root=inputs.attachment_root if inputs is not None else None,
                    )
                except Exception as exc:
                    runtime.warn(f"generate_image output audit unavailable: {type(exc).__name__}")
                    record_tool_evidence(
                        {
                            "attachments": [
                                {
                                    "kind": "output",
                                    "source": "image_generation",
                                    "index": 1,
                                    "status": "ready",
                                    "audit_status": "unrecorded",
                                    "label": "Prepared reference-conditioned image",
                                }
                            ]
                        }
                    )
                else:
                    record_tool_evidence({"attachments": [output_attachment]})
        except asyncio.CancelledError:
            raise
        except DeliveryError:
            raise
        except (ValueError, ImageInputError) as exc:
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
            provenance=provenance,
        )

    return register_tool(dispatcher, generate_image)


__all__ = ["ImageGenerationToolContext", "register_generate_image"]
