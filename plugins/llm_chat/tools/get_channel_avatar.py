"""Acquire current-channel avatars without invoking any visual model."""

from __future__ import annotations

from arclet.entari import Session
from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from ..core.types import JSONType
from ..perception import PerceptionProvider
from ..image_inputs import ImageInputError, current_image_inputs
from ._registration import register_tool
from ..core.tool_trace import record_tool_evidence
from ..core.image_source import fetch_image_bytes


def register_get_channel_avatar(
    dispatcher: PluginDispatcher[JSONType],
    get_perception: PerceptionProvider,
) -> Subscriber[JSONType]:
    """Register immutable avatar acquisition separately from visual inspection."""

    async def get_channel_avatar(participant_ref: str, *, session: Session) -> dict[str, JSONType]:
        """Get one current-channel participant's original avatar as an image_ref, without describing it.

        Use an exact participant_ref obtained from authorized channel context. To see its content,
        call inspect_image; to send it call prepare_image_ref, then send_msg; to modify it call
        edit_image with this source_image_ref. The image_ref expires with the current turn.

        Args:
            participant_ref: Opaque current-channel participant reference.
        """
        inputs = current_image_inputs()
        if inputs is None:
            raise ImageInputError("Avatar acquisition requires an active image-input scope")
        normalized = participant_ref.strip()
        if not normalized:
            return {"available": False, "reason": "participant_ref_required"}
        participant = await get_perception().refresh_participant(session, normalized)
        if participant is None:
            return {"available": False, "reason": "participant_not_found"}
        if not participant.avatar_url:
            return {"available": False, "reason": "avatar_unavailable"}
        source = participant.avatar_url

        async def load() -> bytes:
            data = await fetch_image_bytes(session, source)
            if data is None:
                raise ImageInputError("Avatar pixels could not be acquired")
            return data

        ref = inputs.register(session, source="avatar", key=("avatar", normalized), load=load)
        try:
            snapshot = await inputs.resolve(session, ref, purpose="send")
        except ImageInputError:
            return {"available": False, "image_ref": ref, "reason": "avatar_unavailable"}
        finally:
            record_tool_evidence({"attachments": [inputs.audit_view(ref)]})
        return {
            "available": True,
            "display_name": participant.display_name,
            "participant_ref": normalized,
            "image_ref": ref,
            "source": snapshot.source,
        }

    return register_tool(dispatcher, get_channel_avatar)
