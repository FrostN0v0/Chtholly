"""web_search LLM tool implementation."""

from __future__ import annotations

from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from ..core.types import JSONType
from ..web.policy import WebSearchData, consume_llm_chat_web_access
from ._web_context import WebToolContext
from ._registration import register_tool


def register_web_search(
    dispatcher: PluginDispatcher[JSONType],
    runtime: WebToolContext,
) -> Subscriber[JSONType]:
    """Register public web search using the shared lazy provider client."""

    async def web_search(query: str) -> WebSearchData:
        consume_llm_chat_web_access("web_search")
        data = await runtime.get_client().search(query, max_results=runtime.config.web_search_max_results)
        runtime.log_info(f"web_search returned {len(data['results'])} results")
        return data

    limits = runtime.limits
    web_search.__doc__ = (
        "Search the public web for current or externally verifiable information. Use for explicit search requests or "
        "time-sensitive facts; call read_web_page when snippets are insufficient. Never include secrets, private "
        "profile data, or internal identifiers in the query. "
        f"This generation allows {limits.search_limit} web_search calls, {limits.read_limit} read_web_page calls, "
        f"and {limits.total_limit} total web calls. After any budget exhausted error, stop using web tools and answer "
        "directly from collected evidence, clearly noting anything unverified."
        "\nArgs:\n"
        "    query (str): A concise standalone search query; use site:domain when a specific source is preferred."
    )
    return register_tool(dispatcher, web_search)
