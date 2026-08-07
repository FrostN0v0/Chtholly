"""Shared configuration and lazy client construction for web tools."""

from __future__ import annotations

from typing import Protocol
from dataclasses import dataclass
from collections.abc import Callable

from ..config import LLMChatConfig
from ..web.exa import ExaWebClient
from ..web.policy import WebAccessLimits, normalize_web_access_limits


class WebClientFactory(Protocol):
    def __call__(
        self,
        api_key: str,
        *,
        timeout: float,
        search_type: str,
        category: str | None,
        include_domains: list[str],
        exclude_domains: list[str],
        start_published_date: str | None,
        end_published_date: str | None,
        max_page_chars: int,
    ) -> ExaWebClient: ...


@dataclass
class WebToolContext:
    """Shared normalized limits and lazy Exa client for both web tools."""

    config: LLMChatConfig
    limits: WebAccessLimits
    get_client: Callable[[], ExaWebClient]
    log_info: Callable[[str], object]


def build_web_tool_context(
    config: LLMChatConfig,
    log_info: Callable[[str], object],
    log_warning: Callable[[str], object],
    *,
    client_factory: WebClientFactory = ExaWebClient,
) -> WebToolContext | None:
    """Build enabled web-tool dependencies without opening a client eagerly."""

    limits = normalize_web_access_limits(
        config.web_search_max_calls_per_generation,
        config.web_page_max_calls_per_generation,
        config.web_total_max_calls_per_generation,
    )
    if not config.web_search_enabled:
        log_info("web search tools disabled by configuration")
        return None

    api_key = (config.exa_api_key or "").strip()
    if not api_key or "${{" in api_key or "}}" in api_key:
        log_warning("web search tools disabled: exa_api_key is required")
        return None

    client: ExaWebClient | None = None

    def get_client() -> ExaWebClient:
        nonlocal client
        if client is None:
            client = client_factory(
                api_key,
                timeout=config.web_search_timeout,
                search_type=config.exa_search_type,
                category=config.exa_search_category,
                include_domains=config.exa_include_domains,
                exclude_domains=config.exa_exclude_domains,
                start_published_date=config.exa_start_published_date,
                end_published_date=config.exa_end_published_date,
                max_page_chars=config.web_page_max_chars,
            )
        return client

    return WebToolContext(config=config, limits=limits, get_client=get_client, log_info=log_info)
