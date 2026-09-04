"""edit_image LLM tool implementation."""

from __future__ import annotations

from typing import cast
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
    image_provider_extra,
    image_response_bytes,
    normalize_image_size,
    normalize_image_prompt,
)
from ..core.tool_trace import record_tool_evidence
from ..image_edit_refs import MAX_EDIT_REFERENCES, current_image_edit_references
from ..agent_attachments import store_agent_attachment

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
    """Runtime dependencies and fixed provider policy for reference-conditioned edits."""

    resolve_model: ModelResolver
    edit: ImageProvider
    append_history: HistoryAppender
    warn: WarningSink
    timeout_seconds: float
    quality: ImageQuality
    semaphore: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(1))


def register_edit_image(
    dispatcher: PluginDispatcher[JSONType],
    runtime: ImageEditToolContext,
) -> Subscriber[JSONType]:
    """Register source-preserving image editing with private captured references."""

    async def edit_image(
        session: Session,
        prompt: str,
        source_image_index: int = 1,
        reference_image_refs: list[str] = cast(list[str], None),
        size: ImageSize = DEFAULT_IMAGE_SIZE,
    ) -> str:
        """Edit one current-turn user image and send exactly one result through the configured image model.

        The runtime supplies the selected current user image as the first provider input. Pass only image_ref values
        returned by capture_web_reference in reference_image_refs; those private images follow the source as visual
        identity references and cannot be guessed or reused across generations. When the user explicitly requires a
        real web reference, at least one captured reference is mandatory and generate_image must not be used. State
        precisely what to replace and what source details to preserve. Never place URLs, base64, local paths, secrets,
        internal IDs, or unrelated conversation history in prompt.

        Args:
            prompt (str): Complete source-preserving edit instruction, at most 32000 characters.
            source_image_index (int): One-based current-turn user image index; defaults to 1.
            reference_image_refs (list[str]): Zero to four current-generation refs from capture_web_reference.
            size (str): Output size: 1024x1024, 1536x1024, or 1024x1536.
        Returns:
            str: Confirmed delivery status without exposing provider data or private image references.
        """

        references = current_image_edit_references()
        if references is None:
            raise DeliveryError("image editing is unavailable outside the current llm_chat generation")
        normalized_prompt = normalize_image_prompt(prompt)
        normalized_size = normalize_image_size(size)
        try:
            source = references.resolve_source_image(source_image_index)
            requested_refs = reference_image_refs or []
            if not isinstance(requested_refs, list) or not all(isinstance(item, str) for item in requested_refs):
                raise DeliveryError("reference_image_refs must be a list of captured image references")
            if len(requested_refs) > MAX_EDIT_REFERENCES:
                raise DeliveryError(f"reference_image_refs exceeds the configured limit ({MAX_EDIT_REFERENCES})")
            web_references = references.resolve_web_references(requested_refs)
            if references.requires_web_reference and not web_references:
                raise DeliveryError("this turn requires at least one captured web reference from capture_web_reference")

            model = runtime.resolve_model(session.channel.id)
            provider_images = [source.data, *(reference.data for reference in web_references)]
            async with runtime.semaphore:
                response = await asyncio.wait_for(
                    runtime.edit(
                        model=model.name,
                        prompt=f"{_EDIT_INSTRUCTION}{normalized_prompt}",
                        image=provider_images,
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
            attachment = store_agent_attachment(
                data,
                kind="output",
                source="image_edit",
                index=1,
                label="Edited image result",
                description=(
                    f"Edited source image {source_image_index} with {len(web_references)} captured web reference"
                    f"{'s' if len(web_references) != 1 else ''}."
                ),
                root=references.attachment_root,
            )
            audit_inputs: list[dict[str, object]] = []
            if source.attachment is not None:
                source_attachment = dict(source.attachment)
                source_attachment["label"] = "Source image sent to image model"
                audit_inputs.append(source_attachment)
            for reference_index, reference in enumerate(web_references, start=1):
                if reference.attachment is None:
                    continue
                reference_attachment = dict(reference.attachment)
                reference_attachment["label"] = f"Web reference {reference_index} sent to image model"
                audit_inputs.append(reference_attachment)
            record_tool_evidence(
                {
                    "attachments": [*audit_inputs, attachment],
                    "source_image_index": source_image_index,
                    "reference_count": len(web_references),
                }
            )
        except asyncio.CancelledError:
            raise
        except DeliveryError:
            raise
        except ValueError as exc:
            raise DeliveryError(str(exc)) from None
        except asyncio.TimeoutError:
            runtime.warn("edit_image failed: timeout")
            raise DeliveryError("image editing timed out") from None
        except Exception as exc:
            runtime.warn(f"edit_image failed: {type(exc).__name__}")
            raise DeliveryError("the configured image editing service is unavailable") from None

        result = await deliver_image_bytes(
            session,
            data,
            append_history=runtime.append_history,
            warn=runtime.warn,
            tool_name="edit_image",
            success_message=(
                "Edited image sent successfully. Do not claim another image was sent or expose reference IDs; return "
                "[END_OF_RESPONSE] when no supplement is needed."
            ),
        )
        references.edit_confirmed = True
        return result

    return register_tool(dispatcher, edit_image)


__all__ = ["ImageEditToolContext", "register_edit_image"]
