"""Import-safe Tavily web access boundary for llm_chat."""

from __future__ import annotations

import math
from typing import TypedDict, TypeGuard
import ipaddress
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlsplit, parse_qsl, urlunsplit
from collections.abc import Mapping, Iterator, Sequence
from typing_extensions import Self

import httpx

TAVILY_API_BASE = "https://api.tavily.com"
SEARCH_SNIPPET_MAX_CHARS = 1200
URL_MAX_CHARS = 2048
_QUERY_MAX_CHARS = 500
_BLOCKED_HOSTS = {"localhost", "home.arpa"}
_BLOCKED_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".localdomain",
    ".home",
    ".home.arpa",
    ".lan",
    ".test",
    ".invalid",
    ".example",
    ".onion",
)
_SENSITIVE_QUERY_KEYS = {
    "token",
    "access_token",
    "api_key",
    "key",
    "secret",
    "password",
    "passwd",
    "credential",
    "credentials",
    "auth",
    "authorization",
    "signature",
    "sig",
    "session",
    "session_id",
    "cookie",
}
_SENSITIVE_QUERY_PREFIXES = ("x_amz_", "x_goog_", "oauth_")
_WEB_ACCESS_ALLOWED: ContextVar[bool] = ContextVar("llm_chat_web_access_allowed", default=False)


class WebAccessError(RuntimeError):
    """A sanitized web access validation or provider error."""


class WebSearchItem(TypedDict):
    title: str
    url: str
    snippet: str


class WebSearchData(TypedDict):
    query: str
    results: list[WebSearchItem]


class WebPageData(TypedDict):
    url: str
    content: str


@contextmanager
def llm_chat_web_access_scope() -> Iterator[None]:
    """Allow web access only for the current llm_chat generation context."""

    token = _WEB_ACCESS_ALLOWED.set(True)
    try:
        yield
    finally:
        _WEB_ACCESS_ALLOWED.reset(token)


def require_llm_chat_web_access() -> None:
    """Reject web execution outside the llm_chat generation context."""

    if not _WEB_ACCESS_ALLOWED.get():
        raise WebAccessError("Web access is unavailable outside llm_chat")


def normalize_search_text(value: str, *, field: str) -> str:
    """Normalize a model-provided query or focus without exposing its value."""

    if not isinstance(value, str):
        raise WebAccessError(f"{field} is required")
    normalized = " ".join(value.split())
    if not normalized:
        raise WebAccessError(f"{field} is required")
    return _truncate(normalized, _QUERY_MAX_CHARS)


def normalize_public_url(url: str) -> str:
    """Return a canonical public HTTP(S) URL or reject it safely."""

    if not isinstance(url, str):
        raise WebAccessError("A valid public URL is required")
    candidate = url.strip()
    if not candidate or len(candidate) > URL_MAX_CHARS:
        raise WebAccessError("A valid public URL is required")

    try:
        parsed = urlsplit(candidate)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise WebAccessError("A valid public URL is required") from exc

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not hostname:
        raise WebAccessError("A valid public URL is required")
    if parsed.username is not None or parsed.password is not None:
        raise WebAccessError("A valid public URL is required")
    if hostname.endswith("."):
        raise WebAccessError("A valid public URL is required")

    try:
        ascii_host = hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise WebAccessError("A valid public URL is required") from exc

    _validate_public_host(ascii_host)
    _validate_query_keys(parsed.query)

    if ":" in ascii_host:
        netloc = f"[{ascii_host}]"
    else:
        netloc = ascii_host
    if port is not None:
        netloc = f"{netloc}:{port}"

    normalized = urlunsplit((scheme, netloc, parsed.path, parsed.query, ""))
    if len(normalized) > URL_MAX_CHARS:
        raise WebAccessError("A valid public URL is required")
    return normalized


def _validate_public_host(host: str) -> None:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None

    if address is not None:
        if (
            not address.is_global
            or address.is_loopback
            or address.is_private
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            raise WebAccessError("A valid public URL is required")
        return

    labels = host.split(".")
    if any(not label for label in labels):
        raise WebAccessError("A valid public URL is required")
    if any(
        len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or not all(character.isalnum() or character == "-" for character in label)
        for label in labels
    ):
        raise WebAccessError("A valid public URL is required")
    if all(label.isdecimal() for label in labels):
        raise WebAccessError("A valid public URL is required")
    if len(labels) < 2 or host in _BLOCKED_HOSTS:
        raise WebAccessError("A valid public URL is required")
    if any(host.endswith(suffix) for suffix in _BLOCKED_HOST_SUFFIXES):
        raise WebAccessError("A valid public URL is required")


def _validate_query_keys(query: str) -> None:
    for key, _value in parse_qsl(query.replace(";", "&"), keep_blank_values=True):
        normalized = key.casefold().replace("-", "_")
        if normalized in _SENSITIVE_QUERY_KEYS or normalized.startswith(_SENSITIVE_QUERY_PREFIXES):
            raise WebAccessError("URLs containing sensitive query parameters are not allowed")


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
        raise WebAccessError("Tavily API key is not configured")
    return key


class TavilyWebClient:
    """Minimal Tavily Search and Extract client with sanitized failures."""

    def __init__(
        self,
        api_key: str,
        timeout: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self.timeout = _clamp_timeout(timeout)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def search(self, query: str, *, max_results: int = 5) -> WebSearchData:
        normalized_query = normalize_search_text(query, field="query")
        response = await self._post(
            "/search",
            {
                "query": normalized_query,
                "search_depth": "basic",
                "max_results": _clamp_results(max_results),
                "include_answer": False,
                "include_raw_content": False,
                "include_images": False,
            },
        )
        payload = self._decode_payload(response)
        results = payload.get("results")
        if not _is_non_string_sequence(results):
            raise WebAccessError("Tavily returned an invalid response")

        items: list[WebSearchItem] = []
        seen_urls: set[str] = set()
        for raw_item in results:
            if not isinstance(raw_item, Mapping):
                continue
            raw_title = raw_item.get("title")
            raw_url = raw_item.get("url")
            if not isinstance(raw_title, str) or not isinstance(raw_url, str):
                continue
            title = " ".join(raw_title.split())
            if not title:
                continue
            try:
                normalized_url = normalize_public_url(raw_url)
            except WebAccessError:
                continue
            if normalized_url in seen_urls:
                continue
            raw_content = raw_item.get("content")
            snippet = " ".join(raw_content.split()) if isinstance(raw_content, str) else ""
            items.append(
                {
                    "title": title,
                    "url": normalized_url,
                    "snippet": _truncate(snippet, SEARCH_SNIPPET_MAX_CHARS),
                }
            )
            seen_urls.add(normalized_url)
        return {"query": normalized_query, "results": items}

    async def extract(self, url: str, *, focus: str, max_chars: int = 6000) -> WebPageData:
        normalized_url = normalize_public_url(url)
        normalized_focus = normalize_search_text(focus, field="focus")
        response = await self._post(
            "/extract",
            {
                "urls": [normalized_url],
                "query": normalized_focus,
                "extract_depth": "advanced",
                "include_images": False,
                "format": "markdown",
                "timeout": self.timeout,
            },
        )
        payload = self._decode_payload(response)
        results = payload.get("results")
        if results is None and _is_non_string_sequence(payload.get("failed_results")):
            results = []
        if not _is_non_string_sequence(results):
            raise WebAccessError("Tavily returned an invalid response")

        limit = max(1, int(max_chars))
        for raw_item in results:
            if not isinstance(raw_item, Mapping):
                continue
            raw_url = raw_item.get("url")
            raw_content = raw_item.get("raw_content")
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
        raise WebAccessError("Tavily returned no usable page content")

    async def _post(self, path: str, body: Mapping[str, object]) -> httpx.Response:
        key = _validate_api_key(self._api_key)
        try:
            response = await self._client.post(
                f"{TAVILY_API_BASE}{path}",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                json=dict(body),
                timeout=self.timeout,
            )
        except httpx.TimeoutException as exc:
            raise WebAccessError("Tavily request timed out") from exc
        except httpx.TransportError as exc:
            raise WebAccessError("Tavily service is unavailable") from exc

        if response.status_code == 401:
            raise WebAccessError("Tavily authentication failed")
        if response.status_code in {429, 432, 433}:
            raise WebAccessError("Tavily rate limit or quota exhausted")
        if 400 <= response.status_code < 500:
            raise WebAccessError("Tavily rejected the request")
        if response.status_code >= 500:
            raise WebAccessError("Tavily service is unavailable")
        if not 200 <= response.status_code < 300:
            raise WebAccessError("Tavily returned an invalid response")
        return response

    @staticmethod
    def _decode_payload(response: httpx.Response) -> Mapping[str, object]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise WebAccessError("Tavily returned an invalid response") from exc
        if not isinstance(payload, Mapping):
            raise WebAccessError("Tavily returned an invalid response")
        return payload


def _is_non_string_sequence(value: object) -> TypeGuard[Sequence[object]]:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))
