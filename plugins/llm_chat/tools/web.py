"""Configuration-gated registration for llm_chat web tools."""

from __future__ import annotations

from arclet.entari.logger import log
from arclet.entari.plugin.model import PluginDispatcher

from ..config import LLMChatConfig
from ..web.exa import ExaWebClient
from .web_search import register_web_search
from ..core.types import JSONType
from ._web_context import WebClientFactory, build_web_tool_context
from .read_web_page import register_read_web_page

_LOGGER = log.wrapper("[llm_chat]")


def register_web_access_tools(
    dispatcher: PluginDispatcher[JSONType],
    config: LLMChatConfig,
    *,
    client_factory: WebClientFactory = ExaWebClient,
) -> tuple[str, ...]:
    """Register gated Agno Exa tools on an existing plugin dispatcher."""

    runtime = build_web_tool_context(
        config,
        lambda message: _LOGGER.info(message),
        lambda message: _LOGGER.warning(message),
        client_factory=client_factory,
    )
    if runtime is None:
        return ()
    register_web_search(dispatcher, runtime)
    register_read_web_page(dispatcher, runtime)
    _LOGGER.info("web search tools enabled")
    return ("web_search", "read_web_page")
