"""Prepare original authorized image snapshots without reacquiring their source."""

from __future__ import annotations

from arclet.entari import Image, Session
from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from ..core.types import JSONType
from ..image_inputs import ImageInputError, current_image_inputs
from ._registration import register_tool
from ..prepared_media import prepare_media, ensure_media_capacity
from ..core.tool_trace import record_tool_evidence


def register_prepare_image_ref(dispatcher: PluginDispatcher[JSONType]) -> Subscriber[JSONType]:
    """Register original-image preparation, independently from resource catalog lookup."""

    async def prepare_image_ref(image_ref: str, *, session: Session) -> dict[str, JSONType]:
        """Prepare the exact original snapshot selected by a current-turn image_ref.

        Accept direct, quoted, authorized forwarded/history images, and acquired avatars.
        Do not inspect first merely to send an original. Web and persona references cannot
        be sent by this tool. Preparation does not send: pass the media_ref to send_msg.

        Args:
            image_ref: Exact opaque image input reference from current authorized context.
        """
        inputs = current_image_inputs()
        if inputs is None:
            raise ImageInputError("Image preparation requires an active image-input scope")
        ensure_media_capacity(1, byte_count=0)
        snapshot = await inputs.resolve(session, image_ref, purpose="send")
        record_tool_evidence({"attachments": [inputs.audit_view(image_ref)]})
        return prepare_media(
            session,
            Image.of(raw=snapshot.data, mime=snapshot.mime),
            byte_count=len(snapshot.data),
            tool_name="prepare_image_ref",
            history_marker="[发送了图片]",
        )

    return register_tool(dispatcher, prepare_image_ref)
