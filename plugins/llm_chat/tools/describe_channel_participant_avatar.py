"""describe_channel_participant_avatar LLM tool implementation."""

from __future__ import annotations

import json
from hashlib import sha256
from datetime import datetime

from arclet.entari import Session
from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from ..config import LLMChatConfig
from ..vision import VISION_DESCRIBE_TIMEOUT, vision_completion
from ..core.media import normalize_image_description
from ..core.types import JSONType
from ..perception import PerceptionProvider
from ._registration import register_tool
from ..channel_images import current_channel_image_references
from ..core.image_source import fetch_image_bytes, raw_to_image_data_url

_AVATAR_PROMPT = (
    "Describe only the visible avatar image in concise Simplified Chinese. Mention subject, expression, pose, colors, "
    "style and readable text when present. Do not identify the real person, infer sensitive traits, or speculate "
    "beyond the pixels. "
    "Output one or two plain sentences under 100 Chinese characters."
)


def register_describe_channel_participant_avatar(
    dispatcher: PluginDispatcher[JSONType],
    get_perception: PerceptionProvider,
    config: LLMChatConfig,
) -> Subscriber[JSONType]:
    """Register on-demand avatar refresh and visual description."""

    async def describe_channel_participant_avatar(
        participant_ref: str,
        *,
        session: Session,
    ) -> str:
        """Refresh and visually describe one current-channel participant avatar.

        Call find_channel_participants first when the user refers to someone by name. Pass only its exact
        participant_ref. The result describes the current avatar pixels, not the person's identity or stable traits.
        Avatar data is refreshed from the protocol when possible and cached only while image bytes remain unchanged.
        When image_ref is present, pass it only to send_channel_image if the user asks for the original avatar.
        Never reveal participant_ref, image_ref, avatar URLs, hashes, platform IDs, or cache details to the user.

        Args:
            participant_ref (str): Exact opaque current-channel participant reference.
        Returns:
            str: Compact JSON with display_name, availability, description, and an optional image_ref.
        """

        normalized_ref = participant_ref.strip() if isinstance(participant_ref, str) else ""
        if not normalized_ref:
            return json.dumps(
                {"available": False, "reason": "participant_ref_required"},
                separators=(",", ":"),
            )
        perception = get_perception()
        participant = await perception.refresh_participant(session, normalized_ref)
        if participant is None:
            return json.dumps(
                {"available": False, "reason": "participant_not_found"},
                separators=(",", ":"),
            )
        if not participant.avatar_url:
            return json.dumps(
                {
                    "display_name": participant.display_name,
                    "available": False,
                    "reason": "avatar_unavailable",
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        image_bytes = await fetch_image_bytes(session, participant.avatar_url)
        if image_bytes is None:
            return json.dumps(
                {
                    "display_name": participant.display_name,
                    "available": False,
                    "reason": "avatar_download_failed",
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        data_url = raw_to_image_data_url(image_bytes)
        if data_url is None:
            return json.dumps(
                {
                    "display_name": participant.display_name,
                    "available": False,
                    "reason": "unsupported_avatar_format",
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        references = current_channel_image_references()
        image_ref = (
            references.register_avatar(normalized_ref, participant.avatar_url) if references is not None else None
        )

        def unavailable_result(reason: str) -> str:
            result: dict[str, object] = {
                "display_name": participant.display_name,
                "available": False,
                "reason": reason,
            }
            if image_ref is not None:
                result["image_ref"] = image_ref
            return json.dumps(result, ensure_ascii=False, separators=(",", ":"))

        avatar_hash = sha256(image_bytes).hexdigest()
        if participant.avatar_hash == avatar_hash and participant.avatar_description:
            description = participant.avatar_description
        else:
            try:
                raw = await vision_completion(
                    config,
                    data_url,
                    _AVATAR_PROMPT,
                    "Describe this participant avatar for immediate group-chat context.",
                    timeout=VISION_DESCRIBE_TIMEOUT,
                )
            except Exception:
                return unavailable_result("avatar_vision_failed")
            description = normalize_image_description(raw)
            if not description:
                return unavailable_result("avatar_description_empty")
            await perception.update_avatar(
                session,
                normalized_ref,
                expected_avatar_url=participant.avatar_url,
                avatar_hash=avatar_hash,
                avatar_description=description,
                observed_at=datetime.utcnow(),
            )
        result: dict[str, object] = {
            "display_name": participant.display_name,
            "available": True,
            "description": description,
        }
        if image_ref is not None:
            result["image_ref"] = image_ref
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))

    return register_tool(dispatcher, describe_channel_participant_avatar)
