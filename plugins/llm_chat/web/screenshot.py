"""Bounded public webpage and section screenshot capture."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from playwright.async_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError

from .policy import WebAccessError, normalize_public_url
from .safe_browser import public_browser_page
from .screenshot_dom import (
    settle_page,
    hide_fixed_elements,
    prepare_screenshot_region,
    materialize_screenshot_images,
)
from .screenshot_models import WebScreenshot, WebScreenshotError

if TYPE_CHECKING:
    from entari_plugin_browser import PlaywrightService

DEFAULT_SCREENSHOT_WIDTH = 1280
MIN_SCREENSHOT_WIDTH = 800
MAX_SCREENSHOT_WIDTH = 1440
_SCREENSHOT_VIEWPORT_HEIGHT = 900
_SCREENSHOT_DEVICE_SCALE_FACTOR = 1.5
_SCREENSHOT_PAGE_TIMEOUT_MS = 20_000
_SCREENSHOT_REQUEST_TIMEOUT_SECONDS = 15.0
_SCREENSHOT_SECTION_MAX_CHARS = 200
_SCREENSHOT_LAZY_IMAGE_PASSES = 2
_SCREENSHOT_LAZY_IMAGE_SETTLE_MS = 4000


def normalize_screenshot_width(value: int) -> int:
    if type(value) is not int or not MIN_SCREENSHOT_WIDTH <= value <= MAX_SCREENSHOT_WIDTH:
        raise WebAccessError(f"width must be an integer between {MIN_SCREENSHOT_WIDTH} and {MAX_SCREENSHOT_WIDTH}")
    return value


def normalize_screenshot_section(value: str) -> str:
    if not isinstance(value, str):
        raise WebAccessError("section must be a string")
    normalized = " ".join(value.split())
    if len(normalized) > _SCREENSHOT_SECTION_MAX_CHARS:
        raise WebAccessError(f"section exceeds the configured character limit ({_SCREENSHOT_SECTION_MAX_CHARS})")
    return normalized


async def capture_public_page_screenshot(
    browser: PlaywrightService,
    url: str,
    section: str = "",
    width: int = DEFAULT_SCREENSHOT_WIDTH,
) -> WebScreenshot:
    """Capture one bounded public page overview or visible section."""

    normalized_url = normalize_public_url(url)
    normalized_section = normalize_screenshot_section(section)
    normalized_width = normalize_screenshot_width(width)

    async with public_browser_page(
        browser,
        width=normalized_width,
        height=_SCREENSHOT_VIEWPORT_HEIGHT,
        device_scale_factor=_SCREENSHOT_DEVICE_SCALE_FACTOR,
        request_timeout_seconds=_SCREENSHOT_REQUEST_TIMEOUT_SECONDS,
    ) as (page, proxy):
        page.set_default_timeout(5000)
        try:
            response = await page.goto(
                normalized_url,
                wait_until="domcontentloaded",
                timeout=_SCREENSHOT_PAGE_TIMEOUT_MS,
            )
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            if proxy.main_error is not None:
                raise proxy.main_error from exc
            raise WebScreenshotError("page navigation failed or timed out") from exc
        if proxy.main_error is not None:
            raise proxy.main_error
        if response is None:
            raise WebScreenshotError("page navigation returned no response")
        if response.status >= 400:
            raise WebScreenshotError("page returned an unsuccessful HTTP status")
        try:
            normalize_public_url(page.url)
        except WebAccessError as exc:
            raise WebScreenshotError("page navigated outside the public web") from exc

        try:
            await page.wait_for_load_state("networkidle", timeout=2500)
        except PlaywrightTimeoutError:
            pass
        await settle_page(page)
        proxy.raise_if_budget_exceeded()
        region = await prepare_screenshot_region(page, normalized_section, normalized_width)
        if region is None:
            if normalized_section:
                raise WebScreenshotError("the requested visible page section was not found")
            raise WebScreenshotError("the page did not expose a visible screenshot region")

        await hide_fixed_elements(page)
        for _ in range(_SCREENSHOT_LAZY_IMAGE_PASSES):
            changed_images = await materialize_screenshot_images(page, region)
            await settle_page(page, timeout_ms=_SCREENSHOT_LAZY_IMAGE_SETTLE_MS)
            proxy.raise_if_budget_exceeded()
            refreshed_region = await prepare_screenshot_region(page, normalized_section, normalized_width)
            if refreshed_region is None:
                raise WebScreenshotError("the page lost its visible screenshot region")
            region = refreshed_region
            await hide_fixed_elements(page)
            if changed_images == 0:
                break
        proxy.raise_if_budget_exceeded()
        data = cast(
            bytes,
            await page.screenshot(
                type="png",
                clip={
                    "x": region.x,
                    "y": region.y,
                    "width": region.width,
                    "height": region.height,
                },
                animations="disabled",
                caret="hide",
                scale="device",
            ),
        )
        if not data:
            raise WebScreenshotError("browser returned an empty screenshot")
        return WebScreenshot(data, region.matched, region.truncated)
