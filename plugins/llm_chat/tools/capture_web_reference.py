"""capture_web_reference LLM tool implementation."""

from __future__ import annotations

import json
from typing import Protocol
import asyncio
from secrets import token_hex
from dataclasses import field, dataclass
from collections.abc import Callable, Awaitable

from arclet.entari import Session
from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from ..core.media import normalize_image_description
from ..core.types import JSONType
from ..web.policy import WebAccessError, normalize_search_text, consume_llm_chat_web_access
from ..image_inputs import current_image_inputs
from ._registration import register_tool
from ..core.delivery import DeliveryError
from ..web.screenshot import DEFAULT_SCREENSHOT_WIDTH, normalize_screenshot_width
from ..core.tool_trace import record_tool_evidence
from ..core.media_delivery import MAX_IMAGE_REFERENCES, current_media_requirements
from ..web.reference_capture import WebReferenceCapture, capture_public_reference
from ..web.screenshot_models import WebScreenshotError


class ReferenceCapture(Protocol):
    def __call__(
        self,
        browser: object,
        url: str,
        section: str,
        width: int,
    ) -> Awaitable[WebReferenceCapture]: ...


@dataclass(frozen=True, slots=True)
class ReferenceInspection:
    matched: bool
    description: str


ReferenceDescriber = Callable[[bytes, str], Awaitable[ReferenceInspection]]


@dataclass(slots=True)
class WebReferenceToolContext:
    get_browser: Callable[[], object]
    describe: ReferenceDescriber
    warn: Callable[[str], object]
    capture: ReferenceCapture = capture_public_reference
    semaphore: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(1))


def register_capture_web_reference(
    dispatcher: PluginDispatcher[JSONType],
    runtime: WebReferenceToolContext,
) -> Subscriber[JSONType]:
    """Register private, audited public-web reference image capture."""

    async def capture_web_reference(
        session: Session,
        url: str,
        purpose: str,
        section: str = "",
        width: int = DEFAULT_SCREENSHOT_WIDTH,
    ) -> str:
        """Privately capture and inspect a real public-web visual reference for requested creation or editing.

        Call only when the current user explicitly asks to search or obtain a real web image as a visual reference for
        image generation or editing. Search first, then pass the exact public HTTP(S) result URL. For an HTML page,
        section should be a visible heading or distinctive text near the wanted image; for a direct image URL, leave
        section empty. The tool retains the original pixels privately for this generation, never sends them to the
        user, and returns an image_ref plus a vision description. Pass the matched reference to generate_image for
        new creations or edit_image for source edits. Never expose image_ref to the user.

        Args:
            url (str): Exact public HTTP(S) page or direct image URL selected from current web evidence.
            purpose (str): Short description of the exact visual identity or design details being sought.
            section (str): Optional visible page heading or distinctive text locating the image.
            width (int): Page capture viewport width from 800 to 1440 pixels.
        Returns:
            str: JSON containing a private image_ref and visual description, without local paths or image bytes.
        """

        consume_llm_chat_web_access("capture_web_reference")
        normalized_purpose = normalize_search_text(purpose, field="purpose")
        normalized_width = normalize_screenshot_width(width)
        inputs = current_image_inputs()
        requirements = current_media_requirements()
        if inputs is None or requirements is None or not requirements.intent.requires_web_reference:
            raise DeliveryError("web reference capture is not authorized for the current user turn")

        try:
            async with runtime.semaphore:
                if inputs.source_count("web") >= MAX_IMAGE_REFERENCES:
                    raise DeliveryError("web reference limit reached for the current generation")
                captured = await runtime.capture(runtime.get_browser(), url, section, normalized_width)
                inspection = await runtime.describe(captured.data, normalized_purpose)
            description = normalize_image_description(inspection.description, limit=500)
            if not inspection.matched:
                raise DeliveryError("captured reference did not visibly match the requested subject")
            if not description:
                raise DeliveryError("captured reference image could not be described reliably")
            reference_ref = inputs.register_bytes(
                session,
                source="web",
                key=("web_capture", token_hex(16)),
                data=captured.data,
            )
            snapshot = await inputs.resolve(session, reference_ref, purpose="reference")
            record_tool_evidence(
                {
                    "attachments": [{**inputs.audit_view(snapshot.image_ref), "label": "Web visual reference"}],
                    "source_type": captured.source_type,
                    "matched_section": captured.matched_section,
                    "truncated": captured.truncated,
                }
            )
        except asyncio.CancelledError:
            raise
        except (DeliveryError, WebAccessError):
            raise
        except (ValueError, WebScreenshotError, asyncio.TimeoutError) as exc:
            runtime.warn(f"capture_web_reference failed: {type(exc).__name__}")
            raise DeliveryError("web reference capture failed or did not contain a usable image") from None
        except Exception as exc:
            runtime.warn(f"capture_web_reference failed unexpectedly: {type(exc).__name__}")
            raise DeliveryError("web reference capture service is unavailable") from None

        return json.dumps(
            {
                "available": True,
                "image_ref": reference_ref,
                "description": description,
                "source_type": captured.source_type,
                "matched_section": captured.matched_section,
                "truncated": captured.truncated,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    return register_tool(dispatcher, capture_web_reference)


__all__ = ["ReferenceInspection", "WebReferenceToolContext", "register_capture_web_reference"]
