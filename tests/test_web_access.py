"""Deterministic Agno Exa adapter and web safety tests."""

from __future__ import annotations

import json
from types import ModuleType
import socket
from typing import Any, cast
import asyncio
import importlib
from collections.abc import Sequence

import pytest

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
def web_access_module() -> ModuleType:
    """Load provider and policy modules without Entari runtime registration."""

    policy = importlib.import_module("plugins.llm_chat.web.policy")
    exa = importlib.import_module("plugins.llm_chat.web.exa")
    public_resolver = importlib.import_module("plugins.llm_chat.web.public_resolver")
    screenshot_models = importlib.import_module("plugins.llm_chat.web.screenshot_models")
    safe_browser = importlib.import_module("plugins.llm_chat.web.safe_browser")
    module = ModuleType("plugins.llm_chat.web.test_boundary")
    for source, names in (
        (exa, ("ExaWebClient", "SEARCH_SNIPPET_MAX_CHARS")),
        (
            policy,
            (
                "DEFAULT_WEB_ACCESS_LIMITS",
                "WebAccessError",
                "WebAccessLimits",
                "consume_llm_chat_web_access",
                "llm_chat_web_access_scope",
                "normalize_public_url",
                "normalize_web_access_limits",
                "require_llm_chat_web_access",
            ),
        ),
        (public_resolver, ("PublicResolver",)),
        (screenshot_models, ("WebScreenshotError",)),
        (safe_browser, ("BrowserFetchProxy", "MAX_BROWSER_REQUESTS", "public_browser_page")),
    ):
        for name in names:
            setattr(module, name, getattr(source, name))
    return module


class _FakeExaToolkit:
    def __init__(self, result: object = "[]", error: BaseException | None = None) -> None:
        self.result = result
        self.error = error
        self.search_calls: list[tuple[str, int, str | None]] = []
        self.content_calls: list[list[str]] = []

    def search_exa(self, query: str, num_results: int = 5, category: str | None = None) -> str:
        self.search_calls.append((query, num_results, category))
        if self.error is not None:
            raise self.error
        return cast(str, self.result)

    def get_contents(self, urls: list[str]) -> str:
        self.content_calls.append(list(urls))
        if self.error is not None:
            raise self.error
        return cast(str, self.result)


class _ToolkitFactory:
    def __init__(
        self,
        *,
        search_result: object = "[]",
        content_result: object = "[]",
        search_error: BaseException | None = None,
        content_error: BaseException | None = None,
    ) -> None:
        self.search_toolkit = _FakeExaToolkit(search_result, search_error)
        self.content_toolkit = _FakeExaToolkit(content_result, content_error)
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> _FakeExaToolkit:
        self.calls.append(dict(kwargs))
        return self.search_toolkit if kwargs.get("enable_search") is True else self.content_toolkit


def _make_client(
    module: ModuleType,
    *,
    api_key: str = SENTINEL_API_KEY,
    timeout: float = 30.0,
    search_result: object = "[]",
    content_result: object = "[]",
    search_error: BaseException | None = None,
    content_error: BaseException | None = None,
    **kwargs: object,
) -> tuple[Any, _ToolkitFactory]:
    factory = _ToolkitFactory(
        search_result=search_result,
        content_result=content_result,
        search_error=search_error,
        content_error=content_error,
    )
    client = module.ExaWebClient(
        api_key,
        timeout=timeout,
        toolkit_factory=factory,
        **kwargs,
    )
    return client, factory


def _json_result(items: Sequence[object]) -> str:
    return json.dumps(list(items), ensure_ascii=False)


def _assert_sanitized(error: BaseException) -> None:
    exposed = str(error)
    for sensitive_value in SENSITIVE_VALUES:
        assert sensitive_value not in exposed


async def test_client_builds_separate_agno_exa_toolkits_with_exact_configuration(
    web_access_module: ModuleType,
) -> None:
    client, factory = _make_client(
        web_access_module,
        timeout=999.0,
        search_type="deep",
        category="news",
        include_domains=["reuters.com"],
        exclude_domains=["example.com"],
        start_published_date="2026-01-01",
        end_published_date="2026-12-31",
        max_page_chars=4321,
    )

    assert len(factory.calls) == 2
    search_options, content_options = factory.calls
    assert search_options == {
        "api_key": SENTINEL_API_KEY,
        "enable_find_similar": False,
        "enable_answer": False,
        "enable_research": False,
        "show_results": False,
        "timeout": 60.0,
        "enable_search": True,
        "enable_get_contents": False,
        "text": True,
        "summary": False,
        "text_length_limit": web_access_module.SEARCH_SNIPPET_MAX_CHARS,
        "type": "deep",
        "category": "news",
        "include_domains": ["reuters.com"],
        "exclude_domains": ["example.com"],
        "start_published_date": "2026-01-01",
        "end_published_date": "2026-12-31",
    }
    assert content_options == {
        "api_key": SENTINEL_API_KEY,
        "enable_find_similar": False,
        "enable_answer": False,
        "enable_research": False,
        "show_results": False,
        "timeout": 60.0,
        "enable_search": False,
        "enable_get_contents": True,
        "text": True,
        "summary": False,
        "text_length_limit": 4321,
    }
    assert await client.search("facts") == {"query": "facts", "results": []}


async def test_search_normalizes_and_sanitizes_exa_results(web_access_module: ModuleType) -> None:
    long_snippet = "x" * (web_access_module.SEARCH_SNIPPET_MAX_CHARS + 5)
    client, factory = _make_client(
        web_access_module,
        search_result=_json_result(
            [
                {
                    "title": "  First\n result  ",
                    "url": "HTTPS://News.Example.ORG:443/story?q=ok#top",
                    "text": "  first\n snippet\ttext  ",
                },
                "not-a-mapping",
                {
                    "title": "Private result",
                    "url": "http://127.0.0.1/private",
                    "text": SENTINEL_CONTENT,
                },
                {
                    "title": "Sensitive result URL",
                    "url": "https://drop.example.org/page?access_token=sensitive-value",
                    "text": SENTINEL_CONTENT,
                },
                {
                    "title": "Duplicate canonical URL",
                    "url": "https://news.example.org:443/story?q=ok#other",
                    "text": "must be dropped",
                },
                {
                    "title": "Long result",
                    "url": "https://docs.example.org/item#fragment",
                    "text": long_snippet,
                },
                {
                    "title": None,
                    "url": "https://untitled.example.org/page",
                    "text": None,
                },
                {"title": "Wrong URL type", "url": 123, "text": "drop"},
            ]
        ),
    )

    result = await client.search("  latest\n public\t facts  ", max_results=999)

    assert factory.search_toolkit.search_calls == [("latest public facts", 10, None)]
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
                "title": "https://untitled.example.org/page",
                "url": "https://untitled.example.org/page",
                "snippet": "",
            },
        ],
    }


async def test_search_accepts_empty_results_and_clamps_low_result_count(web_access_module: ModuleType) -> None:
    client, factory = _make_client(web_access_module)

    result = await client.search("facts", max_results=0)

    assert result == {"query": "facts", "results": []}
    assert factory.search_toolkit.search_calls == [("facts", 1, None)]


async def test_query_and_focus_are_normalized_and_capped(web_access_module: ModuleType) -> None:
    client, factory = _make_client(
        web_access_module,
        content_result=_json_result([{"url": "https://source.example.org/page", "text": "body"}]),
    )

    search_result = await client.search(f"  {'q' * 600}\n")
    page_result = await client.extract(
        "https://input.example.org/page",
        focus=f"\t{'f' * 600}  ",
    )

    expected_query = "q" * 499 + "…"
    assert search_result["query"] == expected_query
    assert page_result == {"url": "https://source.example.org/page", "content": "body"}
    assert factory.search_toolkit.search_calls == [(expected_query, 5, None)]
    assert factory.content_toolkit.content_calls == [["https://input.example.org/page"]]


async def test_extract_preserves_content_and_applies_character_limit(web_access_module: ModuleType) -> None:
    raw_content = "  \n# Heading\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\nFinal paragraph.\n  "
    client, factory = _make_client(
        web_access_module,
        content_result=_json_result(
            [
                "not-a-mapping",
                {"url": "https://127.0.0.1/private", "text": "private"},
                {"url": "https://blank.example.org/", "text": "   \n"},
                {"url": "HTTPS://Docs.Example.ORG:443/page#source", "text": raw_content},
            ]
        ),
    )

    result = await client.extract(
        "  HTTPS://Input.Example.ORG:443/article?lang=en#fragment  ",
        focus="  facts\n and\t sections  ",
        max_chars=30,
    )

    assert factory.content_toolkit.content_calls == [["https://input.example.org:443/article?lang=en"]]
    assert result == {
        "url": "https://docs.example.org:443/page",
        "content": "# Heading\n\n| A | B |\n|---|---…",
    }


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(_json_result([]), id="empty-results"),
        pytest.param(_json_result([{"url": "https://source.example.org/page", "text": "   \n"}]), id="blank"),
        pytest.param(_json_result([{"url": "http://127.0.0.1/private", "text": "private"}]), id="private"),
    ],
)
async def test_extract_rejects_results_without_usable_content(
    web_access_module: ModuleType,
    payload: str,
) -> None:
    client, _factory = _make_client(web_access_module, content_result=payload)

    with pytest.raises(web_access_module.WebAccessError) as raised:
        await client.extract("https://input.example.org/page", focus="facts")

    assert str(raised.value) == "Exa returned no usable page content"


@pytest.mark.parametrize(
    ("result", "error", "expected"),
    [
        pytest.param("Error: Operation timed out after 9 seconds", None, "Exa request timed out", id="timeout"),
        pytest.param(f"Error: {SENTINEL_CONTENT}", None, "Exa service is unavailable", id="provider-error"),
        pytest.param("not-json", None, "Exa returned an invalid response", id="invalid-json"),
        pytest.param("{}", None, "Exa returned an invalid response", id="wrong-shape"),
        pytest.param(123, None, "Exa returned an invalid response", id="wrong-type"),
        pytest.param(None, RuntimeError(" | ".join(SENSITIVE_VALUES)), "Exa service is unavailable", id="exception"),
    ],
)
async def test_provider_failures_are_exact_and_sanitized(
    web_access_module: ModuleType,
    result: object,
    error: BaseException | None,
    expected: str,
) -> None:
    client, _factory = _make_client(web_access_module, search_result=result, search_error=error)

    with pytest.raises(web_access_module.WebAccessError) as raised:
        await client.search(SENTINEL_QUERY)

    assert str(raised.value) == expected
    _assert_sanitized(raised.value)


@pytest.mark.parametrize(
    "api_key",
    [
        pytest.param("", id="empty"),
        pytest.param("   \t", id="whitespace"),
        pytest.param("${{ env.get('EXA_API_KEY') }}", id="unresolved-template"),
        pytest.param("prefix }} suffix", id="dangling-template-marker"),
    ],
)
def test_invalid_api_keys_fail_before_toolkit_construction(web_access_module: ModuleType, api_key: str) -> None:
    factory = _ToolkitFactory()

    with pytest.raises(web_access_module.WebAccessError) as raised:
        web_access_module.ExaWebClient(api_key, toolkit_factory=factory)

    assert factory.calls == []
    assert str(raised.value) == "Exa API key is not configured"


@pytest.mark.parametrize(
    ("operation", "expected_error"),
    [
        pytest.param("search", "query is required", id="blank-query"),
        pytest.param("extract", "focus is required", id="blank-focus"),
    ],
)
async def test_blank_search_text_fails_before_tool_execution(
    web_access_module: ModuleType,
    operation: str,
    expected_error: str,
) -> None:
    client, factory = _make_client(web_access_module)

    if operation == "search":
        request = client.search(" \n\t ")
    else:
        request = client.extract("https://public.example.org/page", focus=" \n\t ")
    with pytest.raises(web_access_module.WebAccessError) as raised:
        await request

    assert factory.search_toolkit.search_calls == []
    assert factory.content_toolkit.content_calls == []
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
        pytest.param("https://service.local/page", id="local-suffix"),
        pytest.param("https://service.internal/page", id="internal-suffix"),
        pytest.param("https://service.onion/page", id="onion-suffix"),
        pytest.param("http://127.0.0.1/page", id="loopback-ipv4"),
        pytest.param("http://10.0.0.1/page", id="private-ipv4"),
        pytest.param("http://169.254.1.1/page", id="link-local-ipv4"),
        pytest.param("http://0.0.0.0/page", id="unspecified-ipv4"),
        pytest.param("http://[::1]/page", id="loopback-ipv6"),
        pytest.param("http://[fc00::1]/page", id="private-ipv6"),
        pytest.param("https://public.example.org:not-a-port/page", id="invalid-port"),
        pytest.param("https://[::1/page", id="invalid-ipv6-syntax"),
        pytest.param("https://public.example.org/" + "a" * 2048, id="overlong"),
    ],
)
async def test_rejected_urls_never_reach_exa(web_access_module: ModuleType, url: str) -> None:
    client, factory = _make_client(web_access_module)

    with pytest.raises(web_access_module.WebAccessError) as raised:
        await client.extract(url, focus="public facts")

    assert factory.content_toolkit.content_calls == []
    assert str(raised.value) == "A valid public URL is required"


@pytest.mark.parametrize(
    "query",
    [
        pytest.param("token=sensitive-value", id="token"),
        pytest.param("Access-Token=sensitive-value", id="access-token-normalized"),
        pytest.param("api%5Fkey=sensitive-value", id="encoded-api-key"),
        pytest.param("secret=sensitive-value", id="secret"),
        pytest.param("authorization=sensitive-value", id="authorization"),
        pytest.param("session-id=sensitive-value", id="session-id-normalized"),
        pytest.param("cookie=sensitive-value", id="cookie"),
        pytest.param("X-Amz-Signature=sensitive-value", id="aws-prefix"),
        pytest.param("x-goog-credential=sensitive-value", id="google-prefix"),
        pytest.param("OAuth-Token=sensitive-value", id="oauth-prefix"),
        pytest.param("ok=1;TOKEN=sensitive-value", id="semicolon-separator"),
    ],
)
async def test_sensitive_query_keys_never_reach_exa(web_access_module: ModuleType, query: str) -> None:
    client, factory = _make_client(web_access_module)

    with pytest.raises(web_access_module.WebAccessError) as raised:
        await client.extract(f"https://public.example.org/page?{query}", focus="public facts")

    assert factory.content_toolkit.content_calls == []
    assert str(raised.value) == "URLs containing sensitive query parameters are not allowed"
    assert "sensitive-value" not in str(raised.value)


async def test_public_resolver_pins_public_addresses_and_rejects_mixed_answers(
    web_access_module: ModuleType,
) -> None:
    calls: list[tuple[str, int, socket.AddressFamily]] = []

    async def public_lookup(
        host: str,
        port: int,
        family: socket.AddressFamily,
    ) -> list[tuple[socket.AddressFamily, socket.SocketKind, int, str, tuple[str, int]]]:
        calls.append((host, port, family))
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port))]

    resolver = web_access_module.PublicResolver(public_lookup)
    records = await resolver.resolve("public.example.org", 443, socket.AF_UNSPEC)

    assert calls == [("public.example.org", 443, socket.AF_UNSPEC)]
    assert records == [
        {
            "hostname": "public.example.org",
            "host": "93.184.216.34",
            "port": 443,
            "family": socket.AF_INET,
            "proto": socket.IPPROTO_TCP,
            "flags": socket.AI_NUMERICHOST | socket.AI_NUMERICSERV,
        }
    ]

    async def mixed_lookup(
        _host: str,
        port: int,
        _family: socket.AddressFamily,
    ) -> list[tuple[socket.AddressFamily, socket.SocketKind, int, str, tuple[str, int]]]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", port)),
        ]

    with pytest.raises(web_access_module.WebScreenshotError, match="non-public address"):
        await web_access_module.PublicResolver(mixed_lookup).resolve(
            "rebind.example.org",
            443,
            socket.AF_UNSPEC,
        )


async def test_browser_request_budget_accepts_content_heavy_pages_and_keeps_a_hard_limit(
    web_access_module: ModuleType,
) -> None:
    proxy = web_access_module.BrowserFetchProxy(cast(Any, object()), cast(Any, object()))

    for _ in range(143):
        await proxy._reserve_request(False)

    assert proxy.request_count == 143
    for _ in range(web_access_module.MAX_BROWSER_REQUESTS - 143):
        await proxy._reserve_request(False)
    with pytest.raises(web_access_module.WebScreenshotError, match="request limit"):
        await proxy._reserve_request(False)


async def test_public_browser_page_rethrows_errors_suppressed_by_browser_context(
    web_access_module: ModuleType,
) -> None:
    class PageContext:
        async def route(self, *_args: object) -> None:
            return None

        async def route_web_socket(self, *_args: object) -> None:
            return None

    class Page:
        context = PageContext()

    class SuppressingPageManager:
        async def __aenter__(self) -> Page:
            return Page()

        async def __aexit__(self, *_args: object) -> bool:
            return True

    class Browser:
        def page(self, **_kwargs: object) -> SuppressingPageManager:
            return SuppressingPageManager()

    class CaptureFailure(RuntimeError):
        pass

    with pytest.raises(CaptureFailure):
        async with web_access_module.public_browser_page(
            cast(Any, Browser()),
            width=1280,
            height=900,
            device_scale_factor=1.5,
            request_timeout_seconds=15.0,
        ):
            raise CaptureFailure


def test_web_access_scope_resets_after_normal_exit(web_access_module: ModuleType) -> None:
    with pytest.raises(web_access_module.WebAccessError, match="outside llm_chat"):
        web_access_module.require_llm_chat_web_access()

    with web_access_module.llm_chat_web_access_scope(web_access_module.DEFAULT_WEB_ACCESS_LIMITS):
        assert web_access_module.require_llm_chat_web_access() is None
        with web_access_module.llm_chat_web_access_scope(web_access_module.DEFAULT_WEB_ACCESS_LIMITS):
            assert web_access_module.require_llm_chat_web_access() is None
        assert web_access_module.require_llm_chat_web_access() is None

    with pytest.raises(web_access_module.WebAccessError, match="outside llm_chat"):
        web_access_module.require_llm_chat_web_access()


def test_web_access_scope_resets_after_exception(web_access_module: ModuleType) -> None:
    class ScopeFailure(RuntimeError):
        pass

    def fail_inside_scope() -> None:
        with web_access_module.llm_chat_web_access_scope(web_access_module.DEFAULT_WEB_ACCESS_LIMITS):
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
            with web_access_module.llm_chat_web_access_scope(web_access_module.DEFAULT_WEB_ACCESS_LIMITS):
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


def test_default_web_budget_supports_multi_page_multi_screenshot_tasks(web_access_module: ModuleType) -> None:
    assert web_access_module.DEFAULT_WEB_ACCESS_LIMITS == web_access_module.WebAccessLimits(16, 24, 32)


def test_webpage_screenshot_budget_requires_explicit_current_turn_authorization(
    web_access_module: ModuleType,
) -> None:
    limits = web_access_module.WebAccessLimits(0, 2, 2)

    with web_access_module.llm_chat_web_access_scope(limits):
        with pytest.raises(web_access_module.WebAccessError, match="explicit webpage screenshot request"):
            web_access_module.consume_llm_chat_web_access("screenshot_web_page")
        web_access_module.consume_llm_chat_web_access("read_web_page")
        web_access_module.consume_llm_chat_web_access("read_web_page")

    with web_access_module.llm_chat_web_access_scope(limits, allow_webpage_screenshots=True):
        web_access_module.consume_llm_chat_web_access("screenshot_web_page")
        web_access_module.consume_llm_chat_web_access("read_web_page")


def test_web_access_budget_enforces_specific_and_total_limits(web_access_module: ModuleType) -> None:
    normalized = web_access_module.normalize_web_access_limits(-1, 3, 99)
    assert normalized == web_access_module.WebAccessLimits(0, 3, 3)

    with web_access_module.llm_chat_web_access_scope(normalized):
        with pytest.raises(web_access_module.WebAccessError) as search_error:
            web_access_module.consume_llm_chat_web_access("web_search")
        assert str(search_error.value) == (
            "web_search budget exhausted; answer from collected evidence without more web tools"
        )
        for _ in range(3):
            web_access_module.consume_llm_chat_web_access("read_web_page")
        with pytest.raises(web_access_module.WebAccessError) as total_error:
            web_access_module.consume_llm_chat_web_access("read_web_page")
        assert str(total_error.value) == (
            "Web access budget exhausted; answer from collected evidence without more web tools"
        )

    read_limited = web_access_module.normalize_web_access_limits(2, 1, 99)
    assert read_limited == web_access_module.WebAccessLimits(2, 1, 3)
    with web_access_module.llm_chat_web_access_scope(read_limited):
        web_access_module.consume_llm_chat_web_access("read_web_page")
        with pytest.raises(web_access_module.WebAccessError) as read_error:
            web_access_module.consume_llm_chat_web_access("read_web_page")
        assert str(read_error.value) == (
            "read_web_page budget exhausted; answer from collected evidence without more web tools"
        )
        web_access_module.consume_llm_chat_web_access("web_search")
        web_access_module.consume_llm_chat_web_access("web_search")
        with pytest.raises(web_access_module.WebAccessError) as total_after_rejection:
            web_access_module.consume_llm_chat_web_access("web_search")
        assert str(total_after_rejection.value) == str(total_error.value)

    zero_limits = web_access_module.normalize_web_access_limits(0, 0, 10)
    assert zero_limits == web_access_module.WebAccessLimits(0, 0, 0)
    with web_access_module.llm_chat_web_access_scope(zero_limits):
        with pytest.raises(web_access_module.WebAccessError) as zero_error:
            web_access_module.consume_llm_chat_web_access("web_search")
        assert str(zero_error.value) == str(total_error.value)


async def test_web_access_budget_resets_between_generations_and_shares_with_child_tasks(
    web_access_module: ModuleType,
) -> None:
    limits = web_access_module.WebAccessLimits(1, 0, 1)

    async def isolated_generation() -> None:
        with web_access_module.llm_chat_web_access_scope(limits):
            await asyncio.create_task(asyncio.sleep(0))
            web_access_module.consume_llm_chat_web_access("web_search")
            with pytest.raises(web_access_module.WebAccessError, match="Web access budget exhausted"):
                web_access_module.consume_llm_chat_web_access("web_search")

    await asyncio.gather(isolated_generation(), isolated_generation())

    async def consume_in_child() -> None:
        web_access_module.consume_llm_chat_web_access("web_search")

    async def observe_shared_budget() -> str:
        try:
            web_access_module.consume_llm_chat_web_access("web_search")
        except web_access_module.WebAccessError as exc:
            return str(exc)
        raise AssertionError("child task unexpectedly received a fresh budget")

    with web_access_module.llm_chat_web_access_scope(limits):
        await asyncio.create_task(consume_in_child())
        exhausted = await asyncio.create_task(observe_shared_budget())
        assert exhausted == "Web access budget exhausted; answer from collected evidence without more web tools"

    with web_access_module.llm_chat_web_access_scope(limits):
        web_access_module.consume_llm_chat_web_access("web_search")

    with pytest.raises(web_access_module.WebAccessError, match="outside llm_chat"):
        web_access_module.require_llm_chat_web_access()
