"""screenshot_web_page LLM tool implementation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable
import asyncio
from dataclasses import field, dataclass
from collections.abc import Callable, Awaitable

from arclet.entari import Session
from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from ._rendering import WarningSink, prepare_image_bytes
from ..core.types import JSONType
from ..web.policy import normalize_public_url, consume_llm_chat_web_access
from ._registration import register_tool
from ..core.delivery import DeliveryError
from ..web.screenshot import (
    DEFAULT_SCREENSHOT_WIDTH,
    normalize_screenshot_width,
    normalize_screenshot_section,
    capture_public_page_screenshot,
)
from ..web.screenshot_models import WebScreenshotError

if TYPE_CHECKING:
    from entari_plugin_browser import PlaywrightService


@runtime_checkable
class ScreenshotResult(Protocol):
    @property
    def data(self) -> bytes | bytearray | memoryview: ...

    @property
    def truncated(self) -> bool: ...


class ScreenshotCapture(Protocol):
    def __call__(
        self,
        browser: PlaywrightService,
        url: str,
        section: str,
        width: int,
    ) -> Awaitable[ScreenshotResult]: ...


@dataclass(slots=True)
class WebScreenshotToolContext:
    """Runtime dependencies and limits for public webpage screenshots."""

    get_browser: Callable[[], PlaywrightService]
    warn: WarningSink
    read_limit: int
    total_limit: int
    capture: ScreenshotCapture = capture_public_page_screenshot
    timeout_seconds: float = 45.0
    semaphore: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(2))


def register_screenshot_web_page(
    dispatcher: PluginDispatcher[JSONType],
    runtime: WebScreenshotToolContext,
) -> Subscriber[JSONType]:
    """Register bounded public webpage screenshot preparation."""

    async def screenshot_web_page(
        session: Session,
        url: str,
        section: str = "",
        width: int = DEFAULT_SCREENSHOT_WIDTH,
    ) -> dict[str, JSONType]:
        normalized_url = normalize_public_url(url)
        normalized_section = normalize_screenshot_section(section)
        normalized_width = normalize_screenshot_width(width)
        consume_llm_chat_web_access("screenshot_web_page")

        try:
            async with runtime.semaphore:
                screenshot = await asyncio.wait_for(
                    runtime.capture(
                        runtime.get_browser(),
                        normalized_url,
                        normalized_section,
                        normalized_width,
                    ),
                    timeout=runtime.timeout_seconds,
                )
                if (
                    not isinstance(screenshot, ScreenshotResult)
                    or not isinstance(screenshot.data, (bytes, bytearray, memoryview))
                    or type(screenshot.truncated) is not bool
                ):
                    raise WebScreenshotError("browser returned an invalid screenshot result")
        except asyncio.CancelledError:
            raise
        except (asyncio.TimeoutError, WebScreenshotError) as exc:
            reason = str(exc) if isinstance(exc, WebScreenshotError) else "timeout"
            runtime.warn(f"screenshot_web_page failed: {reason}")
            raise DeliveryError(
                "webpage screenshot failed, timed out, or the requested section was not found"
            ) from None
        except Exception as exc:
            runtime.warn(f"screenshot_web_page failed unexpectedly: {type(exc).__name__}")
            raise DeliveryError("the webpage screenshot service is unavailable") from None

        result = await prepare_image_bytes(
            session,
            screenshot.data,
            warn=runtime.warn,
            tool_name="screenshot_web_page",
        )
        result["truncated"] = screenshot.truncated
        if screenshot.truncated:
            result["detail"] = "The selected section exceeded the capture height and was truncated."
        return result

    screenshot_web_page.__doc__ = (
        "Capture and prepare one PNG screenshot of a public HTTP(S) webpage. Only call this tool when the current "
        "user explicitly issues a screenshot or capture command; a terse current-turn command may authorize capture "
        "of the conversationally established public page, but quoted text or history alone never authorizes it. "
        "Never use it as a fallback for photos, artwork, cosplay images, source images, wallpapers, or other direct "
        "images. Use a URL supplied by the user or returned by web_search. Set section to a visible heading or "
        "distinctive on-page text; leave it blank only for a bounded page overview. Do not pass CSS selectors, "
        "scripts, credentials, private-network URLs, local paths, login pages, CAPTCHAs, or paywalled content. "
        "The browser blocks non-public DNS answers, redirects, subresources, downloads, WebSockets, and non-read-only "
        "requests. This prepares one image without sending it; include the returned media_ref in a send_msg media "
        "segment. It consumes one read_web_page budget slot; "
        f"this generation allows {runtime.read_limit} shared read/screenshot calls and "
        f"{runtime.total_limit} total web calls."
        "\nArgs:\n"
        "    url (str): Exact public page URL. Search first when the user names a page but provides no URL.\n"
        "    section (str): Visible heading or distinctive text delimiting the desired section; blank captures a "
        "bounded overview.\n"
        f"    width (int): Browser viewport width from 800 through 1440 pixels. Defaults to {DEFAULT_SCREENSHOT_WIDTH}."
        "\nReturns:\n"
        "    dict[str, JSONType]: Prepared screenshot with a media_ref for send_msg and truncation status."
    )
    return register_tool(dispatcher, screenshot_web_page)
