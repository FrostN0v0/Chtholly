"""edit_image LLM tool implementation."""

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
    image_provider_extra,
    image_response_bytes,
    normalize_image_size,
    normalize_image_prompt,
)
from ..core.tool_trace import record_tool_evidence
from ..agent_attachments import store_agent_attachment
from ..core.media_delivery import MAX_IMAGE_REFERENCES, ImageProvenance, current_media_requirements

_EDIT_INSTRUCTION = (
    "The first input image is the source composition to edit. When replacing a person or character, replace all "
    "source-specific identity traits, including face, hair, clothing, headwear, and accessories, with traits supported "
    "by the reference images. Preserve the source's exact pose, hand gestures, expression, gaze and eye-closure "
    "orientation, framing, background, typography, logos, and all unrelated visual details unless the requested edit "
    "explicitly changes them. Any subsequent input images are identity and appearance references only: do not copy "
    "their background, text, framing, watermarks, or unrelated objects. Produce exactly one finished image and no "
    "explanatory text inside the image unless requested.\n\nRequested edit:\n"
)


@dataclass(slots=True)
class ImageEditToolContext:
    """Runtime dependencies and fixed provider policy for source-preserving edits."""

    resolve_model: ModelResolver
    edit: ImageProvider
    warn: WarningSink
    timeout_seconds: float
    quality: ImageQuality
    semaphore: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(1))


def register_edit_image(
    dispatcher: PluginDispatcher[JSONType],
    runtime: ImageEditToolContext,
) -> Subscriber[JSONType]:
    """Register source-preserving image editing with immutable authorized inputs."""

    async def edit_image(
        session: Session,
        prompt: str,
        source_image_ref: str,
        reference_image_refs: list[str] = cast(list[str], None),
        size: ImageSize = DEFAULT_IMAGE_SIZE,
        use_persona_reference: bool = False,
    ) -> dict[str, JSONType]:
        """Edit an authorized source image and prepare its result without sending it.

        source_image_ref is required: select the exact incoming, forwarded, channel-history or avatar image_ref.
        Its original pixels are always the first provider input. Additional reference_image_refs supply actual
        authorized visual references, never descriptions. Web and persona references cannot be source images.
        When the user requires a real web reference, include at least one from capture_web_reference.
        Set use_persona_reference=true to upload the configured persona reference after the other inputs.
        Do not put paths, URLs, base64, secrets or unrelated history in prompt. Pass the returned media_ref to
        send_msg to deliver the result; preparing it alone does not complete the request.

        Args:
            prompt: Complete source-preserving instruction, at most 32000 characters.
            source_image_ref: Required current-generation source image_ref.
            reference_image_refs: Zero to four current-generation visual reference image_refs.
            size: Output size: 1024x1024, 1536x1024, or 1024x1536.
            use_persona_reference: Upload the current persona's configured identity reference.
        """
        inputs = current_image_inputs()
        if inputs is None:
            raise DeliveryError("image editing is unavailable outside the current llm_chat generation")
        normalized_prompt = normalize_image_prompt(prompt)
        normalized_size = normalize_image_size(size)
        try:
            source = await inputs.resolve(session, source_image_ref, purpose="edit")
            requested_refs = [] if reference_image_refs is None else reference_image_refs
            if not isinstance(requested_refs, list) or not all(isinstance(item, str) for item in requested_refs):
                raise DeliveryError("reference_image_refs must be a list of authorized image references")
            if len(requested_refs) > MAX_IMAGE_REFERENCES:
                raise DeliveryError(f"reference_image_refs exceeds the configured limit ({MAX_IMAGE_REFERENCES})")
            references = [await inputs.resolve(session, ref, purpose="reference") for ref in requested_refs]
            if use_persona_reference:
                references.append(await inputs.resolve_persona(session))
            provenance = ImageProvenance(
                "edited",
                source_image_ref=source.image_ref,
                reference_image_refs=tuple(reference.image_ref for reference in references),
                reference_sources=tuple(reference.source for reference in references),
            )
            requirements = current_media_requirements()
            if requirements is not None and not requirements.accepts(provenance):
                raise DeliveryError("this turn requires at least one matched web reference from capture_web_reference")
            model = runtime.resolve_model(session.channel.id)
            record_tool_evidence(
                {
                    "attachments": [
                        {
                            **inputs.audit_view(image.image_ref),
                            "label": "Source image sent to image model"
                            if index == 1
                            else f"Visual reference {index - 1} sent to image model",
                            "provider_index": index,
                        }
                        for index, image in enumerate((source, *references), start=1)
                    ],
                    "reference_count": len(references),
                }
            )
            async with runtime.semaphore:
                response = await asyncio.wait_for(
                    runtime.edit(
                        model=model.name,
                        prompt=f"{_EDIT_INSTRUCTION}{normalized_prompt}",
                        image=[source.data, *(reference.data for reference in references)],
                        api_key=model.api_key,
                        api_base=model.base_url,
                        timeout=runtime.timeout_seconds,
                        n=1,
                        size=normalized_size,
                        quality=runtime.quality,
                        input_fidelity="high",
                        response_format="b64_json",
                        max_retries=0,
                        **image_provider_extra(model),
                    ),
                    timeout=runtime.timeout_seconds,
                )
            data = await image_response_bytes(session, response)
            try:
                attachment = store_agent_attachment(
                    data,
                    kind="output",
                    source="image_edit",
                    index=1,
                    label="Prepared edited image",
                    root=inputs.attachment_root,
                )
            except Exception as exc:
                runtime.warn(f"edit_image output audit unavailable: {type(exc).__name__}")
                record_tool_evidence(
                    {
                        "attachments": [
                            {
                                "kind": "output",
                                "source": "image_edit",
                                "index": 1,
                                "status": "ready",
                                "audit_status": "unrecorded",
                                "label": "Prepared edited image",
                            }
                        ]
                    }
                )
            else:
                record_tool_evidence({"attachments": [attachment]})
        except asyncio.CancelledError:
            raise
        except DeliveryError:
            raise
        except (ValueError, ImageInputError) as exc:
            raise DeliveryError(str(exc)) from None
        except asyncio.TimeoutError:
            runtime.warn("edit_image failed: timeout")
            raise DeliveryError("image editing timed out") from None
        except Exception as exc:
            runtime.warn(f"edit_image failed: {type(exc).__name__}")
            raise DeliveryError("the configured image editing service is unavailable") from None

        return await prepare_image_bytes(
            session,
            data,
            warn=runtime.warn,
            tool_name="edit_image",
            provenance=provenance,
        )

    return register_tool(dispatcher, edit_image)


__all__ = ["ImageEditToolContext", "register_edit_image"]
