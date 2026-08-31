"""tag_image LLM tool implementation."""

from __future__ import annotations

import time
import asyncio
from dataclasses import dataclass
from collections.abc import Callable, Sequence, Awaitable

from arclet.entari import Image, Session
from arclet.letoderea import Subscriber
from arclet.entari.logger import log
from arclet.entari.plugin.model import PluginDispatcher

from ..config import LLMChatConfig
from ..core.types import JSONType
from ..meme_store import MemeImportError, MemeImportResult
from ..core.errors import summarize_exception
from ..agent_events import settle_background_tool_result
from ._registration import register_tool
from ..agent_context import current_agent_access
from ..core.delivery import current_llm_chat_delivery
from ..core.tool_trace import current_tool_execution_ref

ImageCollector = Callable[[Session], Sequence[tuple[Image, bool]]]
MemeImporter = Callable[[LLMChatConfig, Session, Image], Awaitable[MemeImportResult]]

_LOGGER = log.wrapper("[llm_chat.tag_image]")
_PENDING_COLLECTIONS: set[asyncio.Task[None]] = set()


@dataclass
class TagImageToolContext:
    """Mutable dependencies for generation-scoped meme collection."""

    config: LLMChatConfig
    collect_images: ImageCollector
    import_image: MemeImporter
    timeout_seconds: float = 15.0


def _result_message(result: MemeImportResult) -> str:
    if result.status == "created":
        return "Collected the current image as a reusable meme."
    if result.status == "duplicate":
        return "The current image is already in the meme collection."
    return "The existing meme now has searchable tags."


async def _update_background_audit(
    *,
    turn_id: int | None,
    execution_ref: str,
    status: str,
    effect: str,
    outcome: dict[str, object],
    started: float,
) -> None:
    if turn_id is None or not execution_ref:
        return
    try:
        updated = await settle_background_tool_result(
            turn_id,
            execution_ref,
            status=status,
            effect=effect,
            result=outcome,
            duration_ms=round((time.monotonic() - started) * 1000),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _LOGGER.warning(f"background image collection audit failed: {summarize_exception(exc)}")
        return
    if not updated:
        _LOGGER.warning("background image collection finished before its audit event became available")


async def _settle_background_collection(
    import_task: asyncio.Task[MemeImportResult],
    *,
    turn_id: int | None,
    execution_ref: str,
    started: float,
) -> None:
    try:
        result = await import_task
    except asyncio.CancelledError:
        audit_task = asyncio.create_task(
            _update_background_audit(
                turn_id=turn_id,
                execution_ref=execution_ref,
                status="cancelled",
                effect="none",
                outcome={"error_code": "cancelled", "error": "Background image collection was cancelled"},
                started=started,
            )
        )
        try:
            await asyncio.shield(audit_task)
        except asyncio.CancelledError:
            await audit_task
        raise
    except Exception as exc:
        outcome: dict[str, object] = {
            "error_code": "execution_failed",
            "error": summarize_exception(exc),
        }
        status = "failed"
        effect = "none"
        _LOGGER.warning(f"background image collection failed: {summarize_exception(exc)}")
    else:
        outcome = {"status": result.status, "summary": _result_message(result)}
        status = "succeeded"
        effect = "confirmed"
        _LOGGER.info(f"background image collection completed: {result.status}")
    await _update_background_audit(
        turn_id=turn_id,
        execution_ref=execution_ref,
        status=status,
        effect=effect,
        outcome=outcome,
        started=started,
    )


def _continue_in_background(
    import_task: asyncio.Task[MemeImportResult],
    *,
    turn_id: int | None,
    execution_ref: str,
    started: float,
) -> None:
    task = asyncio.create_task(
        _settle_background_collection(
            import_task,
            turn_id=turn_id,
            execution_ref=execution_ref,
            started=started,
        ),
        name=f"llm-chat-tag-image:{execution_ref or 'untracked'}",
    )
    _PENDING_COLLECTIONS.add(task)
    task.add_done_callback(_PENDING_COLLECTIONS.discard)


def cancel_pending_image_collections() -> None:
    """Cancel detached image imports during plugin unload or hot reload."""

    for task in tuple(_PENDING_COLLECTIONS):
        task.cancel()


def register_tag_image(
    dispatcher: PluginDispatcher[JSONType],
    runtime: TagImageToolContext,
) -> Subscriber[JSONType]:
    """Register generation-scoped meme collection."""

    async def tag_image(session: Session, image_index: int = 1) -> JSONType:
        """Collect one reusable meme, reaction image, or sticker from the current direct or replied images.

        Use only for an image already attached directly to the current message or its hydrated reply when it is
        clearly reusable as an emotional reaction, reply scene, sticker, or meme. image_index is a 1-based index over
        all direct images first and then all replied images. Never use this for generated or sent images, bare
        unavailable markers, ordinary or sensitive images, or images inside forwarded messages.

        Args:
            image_index (int): Optional 1-based direct-then-replied image index. Defaults to 1.
        Returns:
            JSONType: Privacy-safe collection status without paths, tags, hashes, or database details.
        """

        if current_llm_chat_delivery() is None:
            raise MemeImportError("Image collection is unavailable outside an active llm_chat generation")
        if type(image_index) is not int or image_index < 1:
            raise MemeImportError("image_index must be a positive 1-based integer")

        candidates = runtime.collect_images(session)
        if image_index > len(candidates):
            raise MemeImportError("image_index does not identify a current direct or replied image")

        async def run_import() -> MemeImportResult:
            return await runtime.import_image(runtime.config, session, candidates[image_index - 1][0])

        started = time.monotonic()
        import_task = asyncio.create_task(
            run_import(),
            name="llm-chat-tag-image-import",
        )
        try:
            result = await asyncio.wait_for(
                asyncio.shield(import_task),
                timeout=runtime.timeout_seconds,
            )
        except asyncio.TimeoutError:
            access = current_agent_access()
            _continue_in_background(
                import_task,
                turn_id=access.turn_id if access is not None else None,
                execution_ref=current_tool_execution_ref(),
                started=started,
            )
            return {
                "status": "pending",
                "message": (
                    "Image collection continues in the background. Continue the reply without retrying tag_image "
                    "or claiming it was saved."
                ),
            }
        except asyncio.CancelledError:
            import_task.cancel()
            try:
                await import_task
            except asyncio.CancelledError:
                pass
            raise
        return _result_message(result)

    return register_tool(dispatcher, tag_image)
