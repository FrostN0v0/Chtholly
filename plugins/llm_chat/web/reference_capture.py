"""Private public-web image capture for reference-conditioned image editing."""

from __future__ import annotations

from typing import Any
from dataclasses import dataclass
from urllib.parse import urljoin

from aiohttp import TCPConnector, ClientSession, ClientTimeout

from .policy import WebAccessError, normalize_public_url
from .screenshot import DEFAULT_SCREENSHOT_WIDTH, capture_public_page_screenshot
from .public_resolver import PublicResolver
from .screenshot_models import WebScreenshotError
from ..core.image_source import IMAGE_FETCH_MAX_BYTES, raw_to_image_data_url

_MAX_REDIRECTS = 6
_RESPONSE_CHUNK_BYTES = 64 * 1024
_DIRECT_IMAGE_TIMEOUT_SECONDS = 15.0
_ALLOWED_METHOD = "GET"


@dataclass(frozen=True, slots=True)
class WebReferenceCapture:
    data: bytes
    mime: str
    source_type: str
    matched_section: bool
    truncated: bool


def _sniff_mime(data: bytes) -> str:
    data_url = raw_to_image_data_url(data)
    return data_url[5:].partition(";")[0].casefold() if data_url is not None else ""


async def _fetch_public_direct_image(url: str) -> tuple[bytes, str] | None:
    current_url = normalize_public_url(url)
    resolver = PublicResolver()
    connector = TCPConnector(resolver=resolver, use_dns_cache=True)
    timeout = ClientTimeout(total=_DIRECT_IMAGE_TIMEOUT_SECONDS)
    try:
        async with ClientSession(
            connector=connector,
            connector_owner=True,
            timeout=timeout,
            auto_decompress=True,
            headers={"Accept": "image/avif,image/webp,image/png,image/jpeg,image/gif;q=0.9,*/*;q=0.1"},
        ) as client:
            for _ in range(_MAX_REDIRECTS + 1):
                async with client.request(_ALLOWED_METHOD, current_url, allow_redirects=False) as response:
                    if 300 <= response.status < 400:
                        location = response.headers.get("Location")
                        if not location:
                            raise WebScreenshotError("public image redirect omitted its target")
                        current_url = normalize_public_url(urljoin(current_url, location))
                        continue
                    if response.status >= 400:
                        raise WebScreenshotError("public image returned an unsuccessful HTTP status")
                    content_type = response.headers.get("Content-Type", "").partition(";")[0].strip().casefold()
                    if content_type.startswith("text/") or content_type in {
                        "application/json",
                        "application/xml",
                        "application/xhtml+xml",
                    }:
                        return None
                    content_length = response.content_length
                    if content_length is not None and content_length > IMAGE_FETCH_MAX_BYTES:
                        raise WebScreenshotError("public image exceeded the size limit")
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(_RESPONSE_CHUNK_BYTES):
                        body.extend(chunk)
                        if len(body) > IMAGE_FETCH_MAX_BYTES:
                            raise WebScreenshotError("public image exceeded the size limit")
                    data = bytes(body)
                    mime = _sniff_mime(data)
                    return (data, mime) if mime else None
            raise WebScreenshotError("public image exceeded the redirect limit")
    finally:
        await resolver.close()


async def capture_public_reference(
    browser: Any,
    url: str,
    section: str = "",
    width: int = DEFAULT_SCREENSHOT_WIDTH,
) -> WebReferenceCapture:
    """Capture a direct public image or a bounded rendered public-page region."""

    try:
        direct = await _fetch_public_direct_image(url)
    except (WebAccessError, WebScreenshotError):
        raise
    except Exception as exc:
        raise WebScreenshotError("public image fetch failed") from exc
    if direct is not None:
        data, mime = direct
        return WebReferenceCapture(data, mime, "direct_image", False, False)

    screenshot = await capture_public_page_screenshot(browser, url, section, width)
    return WebReferenceCapture(
        screenshot.data,
        "image/png",
        "page_capture",
        bool(screenshot.matched_section),
        screenshot.truncated,
    )


__all__ = ["WebReferenceCapture", "capture_public_reference"]
