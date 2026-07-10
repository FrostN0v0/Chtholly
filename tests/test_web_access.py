"""Deterministic HTTP and safety tests for the import-safe Tavily boundary."""

from __future__ import annotations

import sys
import json
from uuid import uuid4
from types import ModuleType
from typing import Any
import asyncio
import logging
from pathlib import Path
import importlib.util
from collections.abc import Callable, Iterator

import httpx
import pytest

WEB_ACCESS_PATH = Path(__file__).resolve().parents[1] / "plugins" / "llm_chat" / "web_access.py"
SENTINEL_API_KEY = "sentinel-api-key-do-not-log"
SENTINEL_QUERY = "sentinel-query-do-not-log"
SENTINEL_FOCUS = "sentinel-focus-do-not-log"
SENTINEL_URL = "https://public.example.org/sentinel-url-do-not-log"
SENTINEL_CONTENT = "sentinel-provider-content-do-not-log"
SENSITIVE_VALUES = (
    SENTINEL_API_KEY,
    SENTINEL_QUERY,
    SENTINEL_FOCUS,
    SENTINEL_URL,
    SENTINEL_CONTENT,
)


@pytest.fixture
def web_access_module() -> Iterator[ModuleType]:
    """Load web_access without importing the Entari-backed llm_chat package."""

    module_name = f"_llm_chat_web_access_test_{uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, WEB_ACCESS_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(module_name, None)


def _make_client(
    module: ModuleType,
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    api_key: str = SENTINEL_API_KEY,
    timeout: float = 30.0,
    injected_timeout: float = 0.125,
) -> tuple[Any, httpx.AsyncClient]:
    injected = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=httpx.Timeout(injected_timeout),
    )
    return module.TavilyWebClient(api_key, timeout=timeout, client=injected), injected


def _assert_request_timeout(request: httpx.Request, expected: float) -> None:
    assert request.extensions["timeout"] == {
        "connect": expected,
        "read": expected,
        "write": expected,
        "pool": expected,
    }


def _assert_sanitized(error: BaseException, caplog: pytest.LogCaptureFixture) -> None:
    exposed = f"{error}\n{caplog.text}"
    for sensitive_value in SENSITIVE_VALUES:
        assert sensitive_value not in exposed


async def test_search_sends_exact_request_and_normalizes_results(web_access_module: ModuleType) -> None:
    long_snippet = "x" * (web_access_module.SEARCH_SNIPPET_MAX_CHARS + 5)
    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        _assert_request_timeout(request, 1.0)
        assert request.method == "POST"
        assert str(request.url) == "https://api.tavily.com/search"
        assert request.headers["authorization"] == f"Bearer {SENTINEL_API_KEY}"
        assert request.headers["content-type"] == "application/json"
        assert json.loads(request.content) == {
            "query": "latest public facts",
            "search_depth": "basic",
            "max_results": 10,
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
        }
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "  First\n result  ",
                        "url": "HTTPS://News.Example.ORG:443/story?q=ok#top",
                        "content": "  first\n snippet\ttext  ",
                    },
                    "not-a-mapping",
                    {
                        "title": "Private result",
                        "url": "http://127.0.0.1/private",
                        "content": SENTINEL_CONTENT,
                    },
                    {
                        "title": "Sensitive result URL",
                        "url": "https://drop.example.org/page?access_token=sensitive-value",
                        "content": SENTINEL_CONTENT,
                    },
                    {
                        "title": "Duplicate canonical URL",
                        "url": "https://news.example.org:443/story?q=ok#other",
                        "content": "must be dropped",
                    },
                    {
                        "title": "  Long\tresult  ",
                        "url": "https://docs.example.org/item#fragment",
                        "content": long_snippet,
                    },
                    {
                        "title": "   ",
                        "url": "https://empty-title.example.org/",
                        "content": "must be dropped",
                    },
                    {
                        "title": "No snippet",
                        "url": "https://third.example.org/page",
                        "content": None,
                    },
                    {"title": 123, "url": "https://wrong-title.example.org/", "content": "drop"},
                    {"title": "Wrong URL type", "url": 123, "content": "drop"},
                ]
            },
        )

    client, injected = _make_client(
        web_access_module,
        handler,
        api_key=f"  {SENTINEL_API_KEY}  ",
        timeout=-50.0,
        injected_timeout=42.0,
    )
    try:
        result = await client.search("  latest\n public\t facts  ", max_results=999)
    finally:
        await injected.aclose()

    assert len(captured_requests) == 1
    assert result == {
        "query": "latest public facts",
        "results": [
            {
                "title": "First result",
                "url": "https://news.example.org:443/story?q=ok",
                "snippet": "first snippet text",
            },
            {
                "title": "Long result",
                "url": "https://docs.example.org/item",
                "snippet": "x" * (web_access_module.SEARCH_SNIPPET_MAX_CHARS - 1) + "…",
            },
            {
                "title": "No snippet",
                "url": "https://third.example.org/page",
                "snippet": "",
            },
        ],
    }


async def test_search_accepts_empty_results_and_clamps_low_result_count(web_access_module: ModuleType) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        _assert_request_timeout(request, 60.0)
        assert json.loads(request.content) == {
            "query": "facts",
            "search_depth": "basic",
            "max_results": 1,
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
        }
        return httpx.Response(200, json={"results": []})

    client, injected = _make_client(web_access_module, handler, timeout=500.0, injected_timeout=0.01)
    try:
        result = await client.search("facts", max_results=0)
    finally:
        await injected.aclose()

    assert calls == 1
    assert result == {"query": "facts", "results": []}


async def test_query_and_focus_are_normalized_and_capped(web_access_module: ModuleType) -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        _assert_request_timeout(request, 7.5)
        bodies.append(json.loads(request.content))
        if request.url.path == "/search":
            return httpx.Response(200, json={"results": []})
        return httpx.Response(
            200,
            json={"results": [{"url": "https://source.example.org/page", "raw_content": "body"}]},
        )

    client, injected = _make_client(web_access_module, handler, timeout=7.5, injected_timeout=0.01)
    try:
        search_result = await client.search(f"  {'q' * 600}\n")
        page_result = await client.extract(
            "https://input.example.org/page",
            focus=f"\t{'f' * 600}  ",
        )
    finally:
        await injected.aclose()

    expected_query = "q" * 499 + "…"
    expected_focus = "f" * 499 + "…"
    assert search_result["query"] == expected_query
    assert page_result == {"url": "https://source.example.org/page", "content": "body"}
    assert bodies[0]["query"] == expected_query
    assert bodies[1]["query"] == expected_focus


async def test_extract_sends_exact_request_and_preserves_markdown(web_access_module: ModuleType) -> None:
    raw_content = "  \n# Heading\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\nFinal paragraph.\n  "
    expected_content = "# Heading\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\nFinal paragraph."
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        _assert_request_timeout(request, 60.0)
        assert request.method == "POST"
        assert str(request.url) == "https://api.tavily.com/extract"
        assert request.headers["authorization"] == f"Bearer {SENTINEL_API_KEY}"
        assert request.headers["content-type"] == "application/json"
        assert json.loads(request.content) == {
            "urls": ["https://input.example.org:443/article?lang=en"],
            "query": "facts and sections",
            "extract_depth": "advanced",
            "include_images": False,
            "format": "markdown",
            "timeout": 60.0,
        }
        return httpx.Response(
            200,
            json={
                "results": [
                    "not-a-mapping",
                    {
                        "url": "https://127.0.0.1/private",
                        "raw_content": "private content must be ignored",
                    },
                    {"url": "https://blank.example.org/", "raw_content": "   \n"},
                    {
                        "url": "HTTPS://Docs.Example.ORG:443/page#source",
                        "raw_content": raw_content,
                    },
                    {
                        "url": "https://later.example.org/page",
                        "raw_content": "later content must not be selected",
                    },
                ]
            },
        )

    client, injected = _make_client(web_access_module, handler, timeout=999.0, injected_timeout=0.125)
    try:
        result = await client.extract(
            "  HTTPS://Input.Example.ORG:443/article?lang=en#fragment  ",
            focus="  facts\n and\t sections  ",
            max_chars=6000,
        )
    finally:
        await injected.aclose()

    assert calls == 1
    assert result == {
        "url": "https://docs.example.org:443/page",
        "content": expected_content,
    }


@pytest.mark.parametrize(
    ("max_chars", "expected"),
    [
        pytest.param(8, "abcdefg…", id="configured-limit"),
        pytest.param(0, "…", id="minimum-one-character"),
    ],
)
async def test_extract_applies_character_limit(
    web_access_module: ModuleType,
    max_chars: int,
    expected: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        _assert_request_timeout(request, 30.0)
        return httpx.Response(
            200,
            json={"results": [{"url": "https://source.example.org/page", "raw_content": "abcdefghij"}]},
        )

    client, injected = _make_client(web_access_module, handler)
    try:
        result = await client.extract("https://input.example.org/page", focus="relevant facts", max_chars=max_chars)
    finally:
        await injected.aclose()

    assert result == {"url": "https://source.example.org/page", "content": expected}


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"results": []}, id="empty-results"),
        pytest.param(
            {"results": [{"url": "https://source.example.org/page", "raw_content": "   \n"}]},
            id="blank-content",
        ),
        pytest.param(
            {
                "results": [{"url": "http://127.0.0.1/private", "raw_content": "private"}],
                "failed_results": [],
            },
            id="invalid-provider-url",
        ),
        pytest.param(
            {
                "results": [],
                "failed_results": [{"url": SENTINEL_URL, "error": SENTINEL_CONTENT}],
            },
            id="failed-results-only",
        ),
    ],
)
async def test_extract_rejects_responses_without_usable_content(
    web_access_module: ModuleType,
    payload: object,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        _assert_request_timeout(request, 30.0)
        return httpx.Response(200, json=payload)

    client, injected = _make_client(web_access_module, handler)
    try:
        with pytest.raises(web_access_module.WebAccessError) as raised:
            await client.extract("https://input.example.org/page", focus="facts")
    finally:
        await injected.aclose()

    assert calls == 1
    assert str(raised.value) == "Tavily returned no usable page content"


@pytest.mark.parametrize(
    ("status", "expected_error"),
    [
        pytest.param(401, "Tavily authentication failed", id="authentication"),
        pytest.param(429, "Tavily rate limit or quota exhausted", id="rate-limit"),
        pytest.param(432, "Tavily rate limit or quota exhausted", id="usage-limit"),
        pytest.param(433, "Tavily rate limit or quota exhausted", id="quota-limit"),
        pytest.param(400, "Tavily rejected the request", id="bad-request"),
        pytest.param(418, "Tavily rejected the request", id="other-client-error"),
        pytest.param(499, "Tavily rejected the request", id="last-client-error"),
        pytest.param(500, "Tavily service is unavailable", id="server-error"),
        pytest.param(503, "Tavily service is unavailable", id="unavailable"),
        pytest.param(302, "Tavily returned an invalid response", id="unexpected-status"),
    ],
)
async def test_http_status_errors_are_exact_and_sanitized(
    web_access_module: ModuleType,
    caplog: pytest.LogCaptureFixture,
    status: int,
    expected_error: str,
) -> None:
    caplog.set_level(logging.DEBUG)

    def handler(request: httpx.Request) -> httpx.Response:
        _assert_request_timeout(request, 12.0)
        return httpx.Response(
            status,
            json={
                "message": SENTINEL_CONTENT,
                "query": SENTINEL_QUERY,
                "url": SENTINEL_URL,
            },
        )

    client, injected = _make_client(web_access_module, handler, timeout=12.0, injected_timeout=0.01)
    try:
        with pytest.raises(web_access_module.WebAccessError) as raised:
            await client.extract(SENTINEL_URL, focus=f"{SENTINEL_QUERY} {SENTINEL_FOCUS}")
    finally:
        await injected.aclose()

    assert str(raised.value) == expected_error
    _assert_sanitized(raised.value, caplog)


@pytest.mark.parametrize(
    ("exception_type", "expected_error"),
    [
        pytest.param(httpx.ReadTimeout, "Tavily request timed out", id="timeout"),
        pytest.param(httpx.ConnectError, "Tavily service is unavailable", id="transport"),
    ],
)
async def test_transport_errors_are_exact_and_sanitized(
    web_access_module: ModuleType,
    caplog: pytest.LogCaptureFixture,
    exception_type: type[httpx.TransportError],
    expected_error: str,
) -> None:
    caplog.set_level(logging.DEBUG)

    def handler(request: httpx.Request) -> httpx.Response:
        _assert_request_timeout(request, 9.0)
        leak_blob = " | ".join(SENSITIVE_VALUES)
        raise exception_type(leak_blob, request=request)

    client, injected = _make_client(web_access_module, handler, timeout=9.0, injected_timeout=0.01)
    try:
        with pytest.raises(web_access_module.WebAccessError) as raised:
            await client.extract(SENTINEL_URL, focus=f"{SENTINEL_QUERY} {SENTINEL_FOCUS}")
    finally:
        await injected.aclose()

    assert str(raised.value) == expected_error
    _assert_sanitized(raised.value, caplog)


@pytest.mark.parametrize(
    "response_kind",
    [
        pytest.param("invalid-json", id="invalid-json"),
        pytest.param("top-level-list", id="top-level-list"),
        pytest.param("results-string", id="results-string"),
        pytest.param("missing-results", id="missing-results"),
    ],
)
async def test_invalid_response_shapes_are_sanitized(
    web_access_module: ModuleType,
    caplog: pytest.LogCaptureFixture,
    response_kind: str,
) -> None:
    caplog.set_level(logging.DEBUG)

    def handler(request: httpx.Request) -> httpx.Response:
        _assert_request_timeout(request, 30.0)
        if response_kind == "invalid-json":
            return httpx.Response(200, content=f"not-json {SENTINEL_CONTENT}".encode())
        if response_kind == "top-level-list":
            return httpx.Response(200, json=[SENTINEL_CONTENT])
        if response_kind == "results-string":
            return httpx.Response(200, json={"results": SENTINEL_CONTENT})
        return httpx.Response(200, json={"message": SENTINEL_CONTENT})

    client, injected = _make_client(web_access_module, handler)
    try:
        with pytest.raises(web_access_module.WebAccessError) as raised:
            await client.search(SENTINEL_QUERY)
    finally:
        await injected.aclose()

    assert str(raised.value) == "Tavily returned an invalid response"
    _assert_sanitized(raised.value, caplog)


@pytest.mark.parametrize(
    "api_key",
    [
        pytest.param("", id="empty"),
        pytest.param("   \t", id="whitespace"),
        pytest.param("${{ env.get('TAVILY_API_KEY') }}", id="unresolved-template"),
        pytest.param("prefix }} suffix", id="dangling-template-marker"),
    ],
)
async def test_invalid_api_keys_fail_before_transport(web_access_module: ModuleType, api_key: str) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not execute
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"results": []})

    client, injected = _make_client(web_access_module, handler, api_key=api_key)
    try:
        with pytest.raises(web_access_module.WebAccessError) as raised:
            await client.search("public facts")
    finally:
        await injected.aclose()

    assert calls == 0
    assert str(raised.value) == "Tavily API key is not configured"


@pytest.mark.parametrize(
    ("operation", "expected_error"),
    [
        pytest.param("search", "query is required", id="blank-query"),
        pytest.param("extract", "focus is required", id="blank-focus"),
    ],
)
async def test_blank_search_text_fails_before_transport(
    web_access_module: ModuleType,
    operation: str,
    expected_error: str,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not execute
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"results": []})

    client, injected = _make_client(web_access_module, handler)
    if operation == "search":
        request = client.search(" \n\t ")
    else:
        request = client.extract("https://public.example.org/page", focus=" \n\t ")
    try:
        with pytest.raises(web_access_module.WebAccessError) as raised:
            await request
    finally:
        await injected.aclose()

    assert calls == 0
    assert str(raised.value) == expected_error


@pytest.mark.parametrize(
    ("raw_url", "expected"),
    [
        pytest.param(
            "  HTTPS://WWW.Example.ORG:443/a%20b?q=ok#fragment  ",
            "https://www.example.org:443/a%20b?q=ok",
            id="hostname-scheme-fragment",
        ),
        pytest.param("http://8.8.8.8/path", "http://8.8.8.8/path", id="global-ipv4"),
        pytest.param(
            "https://[2606:4700:4700::1111]/dns-query#fragment",
            "https://[2606:4700:4700::1111]/dns-query",
            id="global-ipv6",
        ),
        pytest.param(
            "https://BÜCHER.Example.ORG/seite",
            "https://xn--bcher-kva.example.org/seite",
            id="idna-host",
        ),
    ],
)
def test_normalize_public_url_accepts_and_canonicalizes_public_urls(
    web_access_module: ModuleType,
    raw_url: str,
    expected: str,
) -> None:
    assert web_access_module.normalize_public_url(raw_url) == expected


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("", id="empty"),
        pytest.param("ftp://public.example.org/file", id="wrong-scheme"),
        pytest.param("https://user:pass@public.example.org/page", id="userinfo"),
        pytest.param("https://intranet/page", id="single-label"),
        pytest.param("https://localhost/page", id="localhost"),
        pytest.param("https://home.arpa/page", id="home-arpa"),
        pytest.param("https://router.home.arpa/page", id="home-arpa-subdomain"),
        pytest.param("https://service.local/page", id="local-suffix"),
        pytest.param("https://service.internal/page", id="internal-suffix"),
        pytest.param("https://service.localdomain/page", id="localdomain-suffix"),
        pytest.param("https://service.home/page", id="home-suffix"),
        pytest.param("https://service.lan/page", id="lan-suffix"),
        pytest.param("https://service.test/page", id="test-suffix"),
        pytest.param("https://service.invalid/page", id="invalid-suffix"),
        pytest.param("https://service.example/page", id="example-suffix"),
        pytest.param("https://service.onion/page", id="onion-suffix"),
        pytest.param("https://localhost./page", id="localhost-terminal-dot"),
        pytest.param("https://host.internal./page", id="internal-terminal-dot"),
        pytest.param("https://public.example.org./page", id="public-terminal-dot"),
        pytest.param("http://127.0.0.1/page", id="loopback-ipv4"),
        pytest.param("http://10.0.0.1/page", id="private-ipv4"),
        pytest.param("http://169.254.1.1/page", id="link-local-ipv4"),
        pytest.param("http://192.0.2.1/page", id="reserved-ipv4"),
        pytest.param("http://224.0.0.1/page", id="multicast-ipv4"),
        pytest.param("http://0.0.0.0/page", id="unspecified-ipv4"),
        pytest.param("http://127.1/page", id="abbreviated-numeric-ipv4"),
        pytest.param("http://2130706433/page", id="integer-numeric-ipv4"),
        pytest.param("http://0300.0250.0001.0001/page", id="ambiguous-numeric-ipv4"),
        pytest.param("http://[::1]/page", id="loopback-ipv6"),
        pytest.param("http://[fc00::1]/page", id="private-ipv6"),
        pytest.param("http://[fe80::1]/page", id="link-local-ipv6"),
        pytest.param("http://[ff02::1]/page", id="multicast-ipv6"),
        pytest.param("http://[::]/page", id="unspecified-ipv6"),
        pytest.param("http://[2001:db8::1]/page", id="reserved-ipv6"),
        pytest.param("https://public.example.org:not-a-port/page", id="invalid-port"),
        pytest.param("https://[::1/page", id="invalid-ipv6-syntax"),
        pytest.param("https://public.example.org/" + "a" * 2048, id="overlong"),
    ],
)
async def test_rejected_urls_never_reach_transport(web_access_module: ModuleType, url: str) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not execute
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"results": []})

    client, injected = _make_client(web_access_module, handler)
    try:
        with pytest.raises(web_access_module.WebAccessError) as raised:
            await client.extract(url, focus="public facts")
    finally:
        await injected.aclose()

    assert calls == 0
    assert str(raised.value) == "A valid public URL is required"


@pytest.mark.parametrize(
    "query",
    [
        pytest.param("token=sensitive-value", id="token"),
        pytest.param("Access-Token=sensitive-value", id="access-token-normalized"),
        pytest.param("api%5Fkey=sensitive-value", id="encoded-api-key"),
        pytest.param("KEY=sensitive-value", id="key-casefold"),
        pytest.param("secret=sensitive-value", id="secret"),
        pytest.param("password=sensitive-value", id="password"),
        pytest.param("passwd=sensitive-value", id="passwd"),
        pytest.param("credential=sensitive-value", id="credential"),
        pytest.param("credentials=sensitive-value", id="credentials"),
        pytest.param("auth=sensitive-value", id="auth"),
        pytest.param("authorization=sensitive-value", id="authorization"),
        pytest.param("signature=sensitive-value", id="signature"),
        pytest.param("sig=sensitive-value", id="sig"),
        pytest.param("session=sensitive-value", id="session"),
        pytest.param("session-id=sensitive-value", id="session-id-normalized"),
        pytest.param("cookie=sensitive-value", id="cookie"),
        pytest.param("X-Amz-Signature=sensitive-value", id="aws-prefix"),
        pytest.param("x-goog-credential=sensitive-value", id="google-prefix"),
        pytest.param("OAuth-Token=sensitive-value", id="oauth-prefix"),
        pytest.param("ok=1;TOKEN=sensitive-value", id="semicolon-separator"),
    ],
)
async def test_sensitive_query_keys_never_reach_transport(
    web_access_module: ModuleType,
    query: str,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not execute
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"results": []})

    client, injected = _make_client(web_access_module, handler)
    try:
        with pytest.raises(web_access_module.WebAccessError) as raised:
            await client.extract(f"https://public.example.org/page?{query}", focus="public facts")
    finally:
        await injected.aclose()

    assert calls == 0
    assert str(raised.value) == "URLs containing sensitive query parameters are not allowed"
    assert "sensitive-value" not in str(raised.value)


async def test_aclose_closes_owned_internal_client(
    web_access_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CloseSpy:
        def __init__(self) -> None:
            self.close_calls = 0

        async def aclose(self) -> None:
            self.close_calls += 1

    spies: list[CloseSpy] = []

    def make_spy() -> CloseSpy:
        spy = CloseSpy()
        spies.append(spy)
        return spy

    monkeypatch.setattr(web_access_module.httpx, "AsyncClient", make_spy)

    explicit_client = web_access_module.TavilyWebClient(SENTINEL_API_KEY)
    await explicit_client.aclose()
    assert spies[0].close_calls == 1

    async with web_access_module.TavilyWebClient(SENTINEL_API_KEY):
        assert spies[1].close_calls == 0
    assert spies[1].close_calls == 1


async def test_aclose_does_not_close_injected_client(web_access_module: ModuleType) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - no HTTP is expected
        raise AssertionError("transport must not be called")

    injected = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        client = web_access_module.TavilyWebClient(SENTINEL_API_KEY, client=injected)
        await client.aclose()
        assert not injected.is_closed

        async with web_access_module.TavilyWebClient(SENTINEL_API_KEY, client=injected):
            pass
        assert not injected.is_closed
    finally:
        await injected.aclose()

    assert injected.is_closed


def test_web_access_scope_resets_after_normal_exit(web_access_module: ModuleType) -> None:
    with pytest.raises(web_access_module.WebAccessError, match="outside llm_chat"):
        web_access_module.require_llm_chat_web_access()

    with web_access_module.llm_chat_web_access_scope():
        assert web_access_module.require_llm_chat_web_access() is None
        with web_access_module.llm_chat_web_access_scope():
            assert web_access_module.require_llm_chat_web_access() is None
        assert web_access_module.require_llm_chat_web_access() is None

    with pytest.raises(web_access_module.WebAccessError, match="outside llm_chat"):
        web_access_module.require_llm_chat_web_access()


def test_web_access_scope_resets_after_exception(web_access_module: ModuleType) -> None:
    class ScopeFailure(RuntimeError):
        pass

    def fail_inside_scope() -> None:
        with web_access_module.llm_chat_web_access_scope():
            assert web_access_module.require_llm_chat_web_access() is None
            raise ScopeFailure

    with pytest.raises(ScopeFailure):
        fail_inside_scope()

    with pytest.raises(web_access_module.WebAccessError, match="outside llm_chat"):
        web_access_module.require_llm_chat_web_access()


async def test_web_access_scope_resets_after_cancellation(web_access_module: ModuleType) -> None:
    entered = asyncio.Event()
    blocker = asyncio.Event()
    reset_observed = asyncio.Event()

    async def cancellable_work() -> None:
        try:
            with web_access_module.llm_chat_web_access_scope():
                assert web_access_module.require_llm_chat_web_access() is None
                entered.set()
                await blocker.wait()
        except asyncio.CancelledError:
            with pytest.raises(web_access_module.WebAccessError, match="outside llm_chat"):
                web_access_module.require_llm_chat_web_access()
            reset_observed.set()
            raise

    task = asyncio.create_task(cancellable_work())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert reset_observed.is_set()
    with pytest.raises(web_access_module.WebAccessError, match="outside llm_chat"):
        web_access_module.require_llm_chat_web_access()
