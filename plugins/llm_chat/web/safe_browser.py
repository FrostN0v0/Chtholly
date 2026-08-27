"""Pinned public-network transport for browser screenshot pages."""

from __future__ import annotations

import socket
from typing import TYPE_CHECKING
import asyncio
from contextlib import asynccontextmanager
from dataclasses import field, dataclass
from collections.abc import AsyncIterator

from aiohttp import TCPConnector, ClientSession, ClientTimeout
from playwright.async_api import Page, Error as PlaywrightError, Route, Request, WebSocketRoute

from .policy import normalize_public_url
from .public_resolver import PublicResolver
from .screenshot_models import WebScreenshotError

if TYPE_CHECKING:
    from entari_plugin_browser import PlaywrightService

MAX_BROWSER_REQUESTS = 192
MAX_BROWSER_REDIRECTS = 6
MAX_RESOURCE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_RESPONSE_BYTES = 32 * 1024 * 1024
_RESPONSE_CHUNK_BYTES = 64 * 1024
_ALLOWED_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_REQUEST_HEADER_DENYLIST = frozenset(
    {
        "accept-encoding",
        "connection",
        "content-length",
        "host",
        "proxy-authorization",
        "proxy-connection",
        "transfer-encoding",
    }
)
_RESPONSE_HEADER_DENYLIST = frozenset(
    {
        "connection",
        "content-encoding",
        "content-length",
        "keep-alive",
        "proxy-authenticate",
        "set-cookie",
        "transfer-encoding",
        "upgrade",
    }
)


@dataclass(slots=True)
class BrowserFetchProxy:
    """Fulfill every browser request through the pinned public resolver."""

    page: Page
    client: ClientSession
    request_count: int = 0
    redirect_count: int = 0
    total_bytes: int = 0
    main_error: WebScreenshotError | None = None
    _budget_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def route(self, route: Route, request: Request) -> None:
        is_main = request.is_navigation_request() and request.frame == self.page.main_frame
        try:
            await self._reserve_request(is_main)
            normalized_url = normalize_public_url(request.url)
            method = request.method.upper()
            if method not in _ALLOWED_METHODS:
                raise WebScreenshotError("page attempted a non-read-only request")
            headers = await request.all_headers()
            request_headers = {
                name: value for name, value in headers.items() if name.casefold() not in _REQUEST_HEADER_DENYLIST
            }
            request_headers["accept-encoding"] = "gzip, deflate"
            async with self.client.request(
                method,
                normalized_url,
                headers=request_headers,
                allow_redirects=False,
            ) as response:
                content_type = response.headers.get("content-type", "").casefold()
                if (
                    is_main
                    and response.status < 300
                    and content_type
                    and not (content_type.startswith("text/html") or content_type.startswith("application/xhtml+xml"))
                ):
                    raise WebScreenshotError("the requested URL did not return an HTML page")
                body = await self._read_response(response.content.iter_chunked(_RESPONSE_CHUNK_BYTES))
                response_headers = {
                    name: value
                    for name, value in response.headers.items()
                    if name.casefold() not in _RESPONSE_HEADER_DENYLIST
                }
                await route.fulfill(
                    status=response.status,
                    headers=response_headers,
                    body=body,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = exc if isinstance(exc, WebScreenshotError) else WebScreenshotError("page resource fetch failed")
            if is_main and self.main_error is None:
                self.main_error = error
            try:
                await route.abort("blockedbyclient")
            except PlaywrightError:
                return

    async def _reserve_request(self, is_main: bool) -> None:
        async with self._budget_lock:
            self.request_count += 1
            if self.request_count > MAX_BROWSER_REQUESTS:
                raise WebScreenshotError("page exceeded the request limit")
            if is_main:
                self.redirect_count += 1
                if self.redirect_count > MAX_BROWSER_REDIRECTS + 1:
                    raise WebScreenshotError("page exceeded the redirect limit")

    async def _read_response(self, chunks: AsyncIterator[bytes]) -> bytes:
        body = bytearray()
        async for chunk in chunks:
            body.extend(chunk)
            if len(body) > MAX_RESOURCE_BYTES:
                raise WebScreenshotError("page resource exceeded the size limit")
            async with self._budget_lock:
                self.total_bytes += len(chunk)
                if self.total_bytes > MAX_TOTAL_RESPONSE_BYTES:
                    raise WebScreenshotError("page exceeded the total download limit")
        return bytes(body)

    def raise_if_budget_exceeded(self) -> None:
        if self.request_count > MAX_BROWSER_REQUESTS:
            raise WebScreenshotError("page exceeded the request limit")
        if self.redirect_count > MAX_BROWSER_REDIRECTS + 1:
            raise WebScreenshotError("page exceeded the redirect limit")
        if self.total_bytes > MAX_TOTAL_RESPONSE_BYTES:
            raise WebScreenshotError("page exceeded the total download limit")


async def _block_web_socket(route: WebSocketRoute) -> None:
    await route.close(code=1008, reason="WebSocket access is disabled")


@asynccontextmanager
async def public_browser_page(
    browser: PlaywrightService,
    *,
    width: int,
    height: int,
    device_scale_factor: float,
    request_timeout_seconds: float,
) -> AsyncIterator[tuple[Page, BrowserFetchProxy]]:
    """Yield an isolated page whose entire HTTP graph uses pinned public IPs."""

    resolver = PublicResolver()
    connector = TCPConnector(
        resolver=resolver,
        use_dns_cache=False,
        family=socket.AF_UNSPEC,
        limit=8,
        limit_per_host=6,
    )
    timeout = ClientTimeout(
        total=request_timeout_seconds,
        connect=min(10.0, request_timeout_seconds),
        sock_read=min(10.0, request_timeout_seconds),
    )
    async with ClientSession(
        connector=connector,
        timeout=timeout,
        auto_decompress=True,
        trust_env=False,
    ) as client:
        pending_error: BaseException | None = None
        try:
            async with browser.page(
                use_global_context=False,
                without_new_context=False,
                viewport={"width": width, "height": height},
                device_scale_factor=device_scale_factor,
                locale="zh-CN",
                color_scheme="light",
                reduced_motion="reduce",
                accept_downloads=False,
                service_workers="block",
            ) as page:
                proxy = BrowserFetchProxy(page, client)

                async def handle_route(route: Route, request: Request) -> None:
                    await proxy.route(route, request)

                await page.context.route("**/*", handle_route)
                await page.context.route_web_socket("**/*", _block_web_socket)
                try:
                    yield page, proxy
                except BaseException as exc:
                    # graiax currently returns from its page context's finally block, suppressing body errors.
                    pending_error = exc
        except BaseException:
            if pending_error is not None:
                raise pending_error
            raise
        if pending_error is not None:
            raise pending_error
