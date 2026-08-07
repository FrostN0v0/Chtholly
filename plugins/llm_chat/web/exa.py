"""Agno Exa provider adapter for llm_chat web access."""

from __future__ import annotations

import json
import math
from typing import Protocol, TypeGuard
import asyncio
from collections.abc import Mapping, Callable, Sequence

from agno.tools.exa import ExaTools

from .policy import (
    WebPageData,
    WebSearchData,
    WebSearchItem,
    WebAccessError,
    normalize_public_url,
    normalize_search_text,
)

SEARCH_SNIPPET_MAX_CHARS = 1200


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    if limit <= 1:
        return "…"
    return f"{value[: limit - 1]}…"


def _clamp_timeout(value: float) -> float:
    timeout = float(value)
    if not math.isfinite(timeout):
        return 30.0
    return min(60.0, max(1.0, timeout))


def _clamp_results(value: int) -> int:
    return min(10, max(1, int(value)))


def _validate_api_key(api_key: str) -> str:
    key = api_key.strip()
    if not key or "${{" in key or "}}" in key:
        raise WebAccessError("Exa API key is not configured")
    return key


class ExaToolkitProtocol(Protocol):
    def search_exa(self, query: str, num_results: int = 5, category: str | None = None) -> str: ...

    def get_contents(self, urls: list[str]) -> str: ...


class ExaWebClient:
    """Agno Exa search and content adapter with local safety controls."""

    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = 30.0,
        search_type: str = "auto",
        category: str | None = None,
        include_domains: Sequence[str] = (),
        exclude_domains: Sequence[str] = (),
        start_published_date: str | None = None,
        end_published_date: str | None = None,
        max_page_chars: int = 6000,
        toolkit_factory: Callable[..., ExaToolkitProtocol] = ExaTools,
    ) -> None:
        key = _validate_api_key(api_key)
        request_timeout = _clamp_timeout(timeout)
        common_options = {
            "api_key": key,
            "enable_find_similar": False,
            "enable_answer": False,
            "enable_research": False,
            "show_results": False,
            "timeout": request_timeout,
        }
        self._search_tools = toolkit_factory(
            **common_options,
            enable_search=True,
            enable_get_contents=False,
            text=True,
            summary=False,
            text_length_limit=SEARCH_SNIPPET_MAX_CHARS,
            type=search_type,
            category=category,
            include_domains=list(include_domains) or None,
            exclude_domains=list(exclude_domains) or None,
            start_published_date=_optional_text(start_published_date),
            end_published_date=_optional_text(end_published_date),
        )
        self._content_tools = toolkit_factory(
            **common_options,
            enable_search=False,
            enable_get_contents=True,
            text=True,
            summary=False,
            text_length_limit=max(1, int(max_page_chars)),
        )

    async def search(self, query: str, *, max_results: int = 5) -> WebSearchData:
        normalized_query = normalize_search_text(query, field="query")
        raw_results = await self._call_tool(
            self._search_tools.search_exa,
            normalized_query,
            num_results=_clamp_results(max_results),
        )
        results = self._decode_results(raw_results)

        items: list[WebSearchItem] = []
        seen_urls: set[str] = set()
        for raw_item in results:
            if not isinstance(raw_item, Mapping):
                continue
            raw_url = raw_item.get("url")
            if not isinstance(raw_url, str):
                continue
            try:
                normalized_url = normalize_public_url(raw_url)
            except WebAccessError:
                continue
            if normalized_url in seen_urls:
                continue
            raw_title = raw_item.get("title")
            title = " ".join(raw_title.split()) if isinstance(raw_title, str) else ""
            raw_content = raw_item.get("text")
            snippet = " ".join(raw_content.split()) if isinstance(raw_content, str) else ""
            items.append(
                {
                    "title": title or normalized_url,
                    "url": normalized_url,
                    "snippet": _truncate(snippet, SEARCH_SNIPPET_MAX_CHARS),
                }
            )
            seen_urls.add(normalized_url)
        return {"query": normalized_query, "results": items}

    async def extract(self, url: str, *, focus: str, max_chars: int = 6000) -> WebPageData:
        normalized_url = normalize_public_url(url)
        normalize_search_text(focus, field="focus")
        raw_results = await self._call_tool(self._content_tools.get_contents, [normalized_url])
        results = self._decode_results(raw_results)

        limit = max(1, int(max_chars))
        for raw_item in results:
            if not isinstance(raw_item, Mapping):
                continue
            raw_url = raw_item.get("url")
            raw_content = raw_item.get("text")
            if not isinstance(raw_url, str) or not isinstance(raw_content, str):
                continue
            content = raw_content.strip()
            if not content:
                continue
            try:
                result_url = normalize_public_url(raw_url)
            except WebAccessError:
                continue
            return {"url": result_url, "content": _truncate(content, limit)}
        raise WebAccessError("Exa returned no usable page content")

    @staticmethod
    async def _call_tool(function: Callable[..., str], *args: object, **kwargs: object) -> str:
        try:
            result = await asyncio.to_thread(function, *args, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise WebAccessError("Exa service is unavailable") from exc
        if not isinstance(result, str):
            raise WebAccessError("Exa returned an invalid response")
        normalized = result.strip()
        if normalized.casefold().startswith("error:"):
            if "timed out" in normalized.casefold():
                raise WebAccessError("Exa request timed out")
            raise WebAccessError("Exa service is unavailable")
        return normalized

    @staticmethod
    def _decode_results(result: str) -> Sequence[object]:
        try:
            payload: object = json.loads(result)
        except ValueError as exc:
            raise WebAccessError("Exa returned an invalid response") from exc
        if not _is_non_string_sequence(payload):
            raise WebAccessError("Exa returned an invalid response")
        return payload


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _is_non_string_sequence(value: object) -> TypeGuard[Sequence[object]]:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))
