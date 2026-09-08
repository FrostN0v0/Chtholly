"""Configuration-gated registration for managed web-artifact tools."""

from __future__ import annotations

from typing import cast
from collections.abc import Callable

from arclet.entari import local_data, add_service
from arclet.entari.logger import log
from arclet.entari.plugin.model import PluginDispatcher

from utils.web_artifacts_core import ArtifactStore

from ..config import LLMChatConfig
from ._artifacts import ArtifactToolContext
from ._rendering import HistoryAppender
from ..core.types import JSONType
from .send_artifact import register_send_artifact
from ..persona.store import append_message
from .read_web_artifact import register_read_web_artifact
from ..artifacts_runtime import CaptureClient, WebArtifactService, normalize_public_origin
from .list_web_artifacts import register_list_web_artifacts
from .revoke_web_preview import register_revoke_web_preview
from .publish_web_preview import register_publish_web_preview

_LOGGER = log.wrapper("[llm_chat]")
WarningSink = Callable[[str], object]


def register_artifact_tools(
    dispatcher: PluginDispatcher[JSONType],
    config: LLMChatConfig,
    *,
    service: WebArtifactService | None = None,
    store: ArtifactStore | None = None,
    capture_client: CaptureClient | None = None,
    append_history: HistoryAppender = append_message,
    warn: WarningSink | None = None,
) -> list[str]:
    """Register all five artifact tools when a public HTTPS origin is configured."""

    existing_service = getattr(dispatcher, "_llm_chat_artifact_service", None)
    existing_names = getattr(dispatcher, "_llm_chat_artifact_names", None)
    if (
        isinstance(existing_service, WebArtifactService)
        and not existing_service.closed
        and isinstance(existing_names, list)
        and all(isinstance(name, str) for name in existing_names)
    ):
        return cast(list[str], existing_names)

    public_origin = config.web_artifacts_public_url
    if not public_origin.strip():
        return []
    normalized_origin = normalize_public_origin(public_origin)
    warning = warn or _LOGGER.warning

    if service is None:
        root = local_data.get_data_dir("llm_chat") / "web_artifacts"
        service = add_service(
            WebArtifactService(
                root,
                public_origin=normalized_origin,
                capture_url=config.web_artifacts_capture_url,
                capture_token=config.web_artifacts_capture_token,
                ttl_hours=config.web_artifacts_ttl_hours,
                store=store,
                capture_client=capture_client,
                warn=warning,
            )
        )
    elif service.public_origin != normalized_origin:
        raise ValueError("injected web artifact service origin does not match configured origin")

    runtime = ArtifactToolContext(service=service, append_history=append_history, warn=warning)
    register_publish_web_preview(dispatcher, runtime)
    register_send_artifact(dispatcher, runtime)
    register_list_web_artifacts(dispatcher, runtime)
    register_read_web_artifact(dispatcher, runtime)
    register_revoke_web_preview(dispatcher, runtime)
    names = [
        "publish_web_preview",
        "send_artifact",
        "list_web_artifacts",
        "read_web_artifact",
        "revoke_web_preview",
    ]
    setattr(dispatcher, "_llm_chat_artifact_service", service)
    setattr(dispatcher, "_llm_chat_artifact_names", names)
    return names


__all__ = ["register_artifact_tools"]
