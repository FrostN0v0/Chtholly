"""tag_image LLM tool implementation."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable, Sequence, Awaitable

from arclet.entari import Image, Session
from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from ..config import LLMChatConfig
from ..core.types import JSONType
from ..meme_store import MemeImportError, MemeImportResult
from ._registration import register_tool
from ..core.delivery import current_llm_chat_delivery

ImageCollector = Callable[[Session], Sequence[tuple[Image, bool]]]
MemeImporter = Callable[[LLMChatConfig, Session, Image], Awaitable[MemeImportResult]]


@dataclass
class TagImageToolContext:
    """Mutable dependencies for generation-scoped meme collection."""

    config: LLMChatConfig
    collect_images: ImageCollector
    import_image: MemeImporter


def register_tag_image(
    dispatcher: PluginDispatcher[JSONType],
    runtime: TagImageToolContext,
) -> Subscriber[JSONType]:
    """Register generation-scoped meme collection."""

    async def tag_image(session: Session, image_index: int = 1) -> str:
        """Collect one reusable meme, reaction image, or sticker from the current direct or replied images.

        Use only for an image already attached directly to the current message or its hydrated reply when it is
        clearly reusable as an emotional reaction, reply scene, sticker, or meme. image_index is a 1-based index over
        all direct images first and then all replied images. Never use this for generated or sent images, bare
        unavailable markers, ordinary or sensitive images, or images inside forwarded messages.

        Args:
            image_index (int): Optional 1-based direct-then-replied image index. Defaults to 1.
        Returns:
            str: Privacy-safe collection status without paths, tags, hashes, or database details.
        """

        if current_llm_chat_delivery() is None:
            raise MemeImportError("Image collection is unavailable outside an active llm_chat generation")
        if type(image_index) is not int or image_index < 1:
            raise MemeImportError("image_index must be a positive 1-based integer")

        candidates = runtime.collect_images(session)
        if image_index > len(candidates):
            raise MemeImportError("image_index does not identify a current direct or replied image")

        result = await runtime.import_image(runtime.config, session, candidates[image_index - 1][0])
        if result.status == "created":
            return "Collected the current image as a reusable meme."
        if result.status == "duplicate":
            return "The current image is already in the meme collection."
        return "The existing meme now has searchable tags."

    return register_tool(dispatcher, tag_image)
