"""Isolated Playwright capture for immutable artifact previews."""

from __future__ import annotations

import os
import sys
from typing import Any, Protocol
import asyncio
import inspect
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit
from collections.abc import Mapping, Callable, Awaitable

from utils.web_artifacts_core import ArtifactStore

MIN_CAPTURE_WIDTH = 480
MAX_CAPTURE_WIDTH = 1200
DEFAULT_CAPTURE_WIDTH = 900
MAX_CAPTURE_HEIGHT = 4096
MAX_CAPTURE_BYTES = 3 * 1024 * 1024
MAX_CAPTURE_PROJECT_BYTES = 8 * 1024 * 1024
DEFAULT_CAPTURE_WALL_TIME = 25.0
MAX_CAPTURE_WALL_TIME = 30.0
MAX_CAPTURE_QUEUE = 8
DEFAULT_CAPTURE_QUEUE = 2


class ArtifactCaptureError(RuntimeError):
    """Base error for a capture that cannot be safely returned."""


class CaptureTooLarge(ArtifactCaptureError):
    """The rendered PNG or its bounded input exceeds the capture limit."""


class CaptureBusy(ArtifactCaptureError):
    """The single-renderer queue is full."""


class CaptureClosed(ArtifactCaptureError):
    """The renderer is shutting down."""


class CaptureTimedOut(ArtifactCaptureError):
    """The renderer exceeded its wall-time budget."""


class CaptureFailed(ArtifactCaptureError):
    """The renderer returned an invalid or unavailable image."""


class CaptureRenderer(Protocol):
    async def capture(self, token: str, width: int) -> bytes: ...


CaptureLauncher = Callable[[Any, Mapping[str, object]], Awaitable[Any] | Any]


def build_file_csp(resource_prefix: str, frame_origin: str) -> str:
    """Build a prefix-scoped CSP for one artifact's original files."""

    return "; ".join(
        (
            "default-src 'none'",
            "base-uri 'none'",
            "object-src 'none'",
            "script-src 'unsafe-inline' " + resource_prefix,
            "style-src 'unsafe-inline' " + resource_prefix + " data:",
            "img-src " + resource_prefix + " data:",
            "font-src " + resource_prefix + " data:",
            "media-src " + resource_prefix + " data:",
            "connect-src " + resource_prefix,
            "frame-src 'none'",
            "child-src 'none'",
            "worker-src 'none'",
            "manifest-src 'none'",
            "form-action 'none'",
            "frame-ancestors " + frame_origin,
            "sandbox allow-scripts",
        )
    )


def _virtual_asset_path(url: str) -> str | None:
    parsed = urlsplit(url)
    if parsed.scheme.casefold() != "https" or (parsed.hostname or "").casefold() != "artifact.invalid":
        return None
    if parsed.port not in (None, 443):
        return None
    if not parsed.path.startswith("/files/"):
        return None
    path = unquote(parsed.path[len("/files/") :])
    if not path or path.startswith(("/", "\\")) or "\\" in path or "\x00" in path:
        return None
    if any(part in ("", ".", "..") for part in path.split("/")):
        return None
    return path


async def _close_quietly(value: object | None) -> None:
    if value is None:
        return
    close = getattr(value, "close", None)
    if not callable(close):
        return
    try:
        result = close()
        if inspect.isawaitable(result):
            await result
    except BaseException:
        return


class CaptureCoordinator:
    """Serialize captures while keeping the number of waiting callers bounded."""

    def __init__(
        self,
        renderer: CaptureRenderer | Callable[[str, int], Awaitable[bytes]],
        *,
        max_queue: int = DEFAULT_CAPTURE_QUEUE,
        wall_time: float = DEFAULT_CAPTURE_WALL_TIME,
    ) -> None:
        if max_queue < 0 or max_queue > MAX_CAPTURE_QUEUE:
            raise ValueError(f"max_queue must be between 0 and {MAX_CAPTURE_QUEUE}")
        if wall_time <= 0 or wall_time > MAX_CAPTURE_WALL_TIME:
            raise ValueError(f"wall_time must be between 0 and {MAX_CAPTURE_WALL_TIME}")
        self._renderer = renderer
        self._max_queue = int(max_queue)
        self._wall_time = float(wall_time)
        self._condition = asyncio.Condition()
        self._active = False
        self._queued = 0
        self._active_task: asyncio.Task[object] | None = None
        self._closed = False

    @property
    def active(self) -> bool:
        return self._active

    @property
    def queued(self) -> int:
        return self._queued

    async def _invoke(self, token: str, width: int) -> bytes:
        capture = getattr(self._renderer, "capture", None)
        result: object
        if callable(capture):
            result = capture(token, width)
        else:
            result = self._renderer(token, width)  # type: ignore[operator]
        if inspect.isawaitable(result):
            result = await result
        if type(result) is not bytes or not result:
            raise CaptureFailed("renderer did not return PNG bytes")
        return result

    async def capture(self, token: str, width: int) -> bytes:
        task = asyncio.current_task()
        if task is None:
            raise CaptureFailed("capture requires an asyncio task")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._wall_time
        queued = False
        async with self._condition:
            if self._closed:
                raise CaptureClosed("capture coordinator is closed")
            if self._active:
                if self._queued >= self._max_queue:
                    raise CaptureBusy("capture queue is full")
                self._queued += 1
                queued = True
                try:
                    while self._active and not self._closed:
                        await asyncio.wait_for(self._condition.wait(), timeout=max(0.0, deadline - loop.time()))
                    if self._closed:
                        raise CaptureClosed("capture coordinator is closed")
                    self._queued -= 1
                    queued = False
                    self._active = True
                    self._active_task = task
                except BaseException as exc:
                    if queued:
                        self._queued -= 1
                    self._condition.notify_all()
                    if isinstance(exc, asyncio.TimeoutError):
                        raise CaptureTimedOut("capture exceeded wall time while queued") from exc
                    raise
            else:
                self._active = True
                self._active_task = task

        try:
            try:
                return await asyncio.wait_for(self._invoke(token, width), timeout=max(0.0, deadline - loop.time()))
            except asyncio.TimeoutError as exc:
                raise CaptureTimedOut("capture exceeded wall time") from exc
        finally:
            async with self._condition:
                if self._active_task is task:
                    self._active = False
                    self._active_task = None
                    self._condition.notify_all()

    async def close(self) -> None:
        current = asyncio.current_task()
        async with self._condition:
            self._closed = True
            active_task = self._active_task
            self._condition.notify_all()
        if active_task is not None and active_task is not current:
            active_task.cancel()
            try:
                await active_task
            except BaseException:
                pass


class PlaywrightArtifactRenderer:
    """Render registered files in a browser child with OS-level isolation.

    On Linux, the packaged ``chromium-isolated.sh`` wrapper always launches
    Chromium inside ``unshare --user --map-current-user --net`` with a minimal
    environment. Missing or non-executable wrapper/browser files fail closed.
    A caller-provided ``browser_launcher`` is an explicit test/deployment seam;
    it owns its own process isolation policy. Non-Linux hosts retain the
    offline browser context and request interception, but do not claim an OS
    network namespace.
    """

    def __init__(
        self,
        store: ArtifactStore,
        *,
        max_bytes: int = MAX_CAPTURE_BYTES,
        max_height: int = MAX_CAPTURE_HEIGHT,
        browser_launcher: CaptureLauncher | None = None,
    ) -> None:
        if max_bytes <= 0 or max_height <= 0:
            raise ValueError("capture limits must be positive")
        self._store = store
        self._max_bytes = min(int(max_bytes), MAX_CAPTURE_BYTES)
        self._max_height = min(int(max_height), MAX_CAPTURE_HEIGHT)
        self._browser_launcher = browser_launcher

    async def _load_assets(self, token: str) -> tuple[str, dict[str, tuple[bytes, str]]]:
        artifact = await asyncio.to_thread(self._store.get_public, token)
        entry = str(artifact.entry)
        assets: dict[str, tuple[bytes, str]] = {}
        total = 0
        for info in artifact.files:
            path = str(info.path)
            data, mime = await asyncio.to_thread(self._store.read_public_file, token, path)
            if type(data) is not bytes:
                raise CaptureFailed("registered artifact file is unavailable")
            total += len(data)
            if total > MAX_CAPTURE_PROJECT_BYTES:
                raise CaptureTooLarge("artifact inputs exceed capture budget")
            assets[path] = (data, str(mime))
        if entry not in assets:
            raise CaptureFailed("artifact entry is unavailable")
        return entry, assets

    async def _launch_browser(self, playwright: Any, options: Mapping[str, object]) -> Any:
        if self._browser_launcher is not None:
            result = self._browser_launcher(playwright, options)
            if inspect.isawaitable(result):
                return await result
            return result
        launch_options = dict(options)
        if sys.platform.startswith("linux"):
            wrapper = Path(__file__).with_name("chromium-isolated.sh")
            namespace_tool = Path("/usr/bin/unshare")
            executable = str(getattr(playwright.chromium, "executable_path", ""))
            if not wrapper.is_file() or not os.access(wrapper, os.X_OK):
                raise CaptureFailed("network isolation is unavailable")
            if not namespace_tool.is_file() or not os.access(namespace_tool, os.X_OK):
                raise CaptureFailed("network namespace tool is unavailable")
            if not executable or not Path(executable).is_file() or not os.access(executable, os.X_OK):
                raise CaptureFailed("Chromium executable is unavailable")
            env_keys = (
                "HOME",
                "LANG",
                "LC_ALL",
                "LC_CTYPE",
                "PATH",
                "TMPDIR",
                "XDG_CACHE_HOME",
                "XDG_CONFIG_HOME",
                "XDG_RUNTIME_DIR",
                "FONTCONFIG_PATH",
            )
            browser_env = {key: value for key in env_keys if (value := os.environ.get(key))}
            browser_env["WEB_ARTIFACT_CHROMIUM_PATH"] = executable
            launch_options["executable_path"] = str(wrapper)
            launch_options["env"] = browser_env
        return await playwright.chromium.launch(**launch_options)

    async def capture(self, token: str, width: int) -> bytes:
        if not MIN_CAPTURE_WIDTH <= width <= MAX_CAPTURE_WIDTH:
            raise CaptureFailed("capture width is outside the supported range")
        entry, assets = await self._load_assets(token)
        from playwright.async_api import (
            Error as PlaywrightError,
            TimeoutError as PlaywrightTimeoutError,
            async_playwright,
        )

        launch_options: dict[str, object] = {
            "headless": True,
            "chromium_sandbox": True,
            "args": [
                "--disable-background-networking",
                "--disable-component-update",
                "--disable-default-apps",
                "--disable-dev-shm-usage",
                "--disable-extensions",
                "--disable-features=InterestFeedContentSuggestions,MediaRouter,OptimizationHints,Translate,WebRtcHideLocalIpsWithMdns",
                "--disable-quic",
                "--disable-sync",
                "--disable-webrtc",
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                "--no-first-run",
                "--no-pings",
                "--no-service-autorun",
            ],
        }
        virtual_prefix = "https://artifact.invalid/files/"
        virtual_origin = "https://artifact.invalid"
        file_csp = build_file_csp(virtual_prefix, virtual_origin)
        background_tasks: set[asyncio.Future[object]] = set()

        def schedule(awaitable: Awaitable[object]) -> None:
            task = asyncio.ensure_future(awaitable)
            background_tasks.add(task)
            task.add_done_callback(background_tasks.discard)

        async with async_playwright() as playwright:
            browser: Any = None
            context: Any = None
            page: Any = None
            try:
                browser = await self._launch_browser(playwright, launch_options)
                context = await browser.new_context(
                    viewport={"width": width, "height": min(900, self._max_height)},
                    device_scale_factor=1,
                    java_script_enabled=True,
                    service_workers="block",
                    accept_downloads=False,
                    offline=True,
                    ignore_https_errors=False,
                )
                await context.add_init_script(
                    """
                    (() => {
                      try { window.open = () => null; } catch (_) {}
                      try { Object.defineProperty(navigator, 'serviceWorker', { value: undefined }); } catch (_) {}
                    })();
                    """
                )

                async def handle_route(route: Any) -> None:
                    request = route.request
                    if request.method.upper() != "GET":
                        try:
                            await route.abort("blockedbyclient")
                        except Exception:
                            pass
                        return
                    path = _virtual_asset_path(request.url)
                    asset = assets.get(path) if path is not None else None
                    if asset is None:
                        try:
                            await route.abort("blockedbyclient")
                        except Exception:
                            pass
                        return
                    data, mime = asset
                    try:
                        await route.fulfill(
                            status=200,
                            body=data,
                            content_type=mime,
                            headers={
                                "Content-Security-Policy": file_csp,
                                "Cache-Control": "no-store",
                                "X-Content-Type-Options": "nosniff",
                                "Referrer-Policy": "no-referrer",
                                "X-DNS-Prefetch-Control": "off",
                                "Access-Control-Allow-Origin": "*",
                            },
                        )
                    except Exception:
                        return

                await context.route("**/*", handle_route)

                async def handle_web_socket(websocket: Any) -> None:
                    try:
                        await websocket.close()
                    except Exception:
                        pass

                await context.route_web_socket("**/*", handle_web_socket)

                page = await context.new_page()
                page.set_default_timeout(4_000)
                page.set_default_navigation_timeout(9_000)

                def close_download(download: Any) -> None:
                    cancel = getattr(download, "cancel", None)
                    if callable(cancel):
                        try:
                            result = cancel()
                        except Exception:
                            return
                        if inspect.isawaitable(result):
                            schedule(result)

                page.on("download", close_download)

                def dismiss_dialog(dialog: Any) -> None:
                    dismiss = getattr(dialog, "dismiss", None)
                    if callable(dismiss):
                        try:
                            result = dismiss()
                        except Exception:
                            return
                        if inspect.isawaitable(result):
                            schedule(result)

                page.on("dialog", dismiss_dialog)

                async def close_popup(popup: Any) -> None:
                    await _close_quietly(popup)

                def close_new_page(new_page: Any) -> None:
                    if page is not None and new_page is not page:
                        schedule(close_popup(new_page))

                context.on("page", close_new_page)

                async def reset_external_navigation() -> None:
                    if page is None:
                        return
                    try:
                        await page.goto("about:blank", wait_until="commit", timeout=1_000)
                    except Exception:
                        return

                def inspect_navigation(frame: Any) -> None:
                    if frame is not page.main_frame:
                        return
                    parsed = urlsplit(frame.url)
                    if parsed.scheme.casefold() == "https" and (parsed.hostname or "").casefold() == "artifact.invalid":
                        return
                    schedule(reset_external_navigation())

                page.on("framenavigated", inspect_navigation)
                target_url = virtual_prefix + quote(entry, safe="/")
                await page.goto(target_url, wait_until="domcontentloaded", timeout=9_000)
                try:
                    await page.wait_for_load_state("load", timeout=3_000)
                except (PlaywrightError, PlaywrightTimeoutError):
                    pass
                try:
                    await asyncio.wait_for(
                        page.wait_for_function(
                            """() => Array.from(document.images || []).every((image) => image.complete)""",
                            timeout=3_000,
                        ),
                        timeout=3.5,
                    )
                except (asyncio.TimeoutError, PlaywrightError, PlaywrightTimeoutError):
                    pass
                try:
                    await asyncio.wait_for(
                        page.evaluate(
                            """async () => {
                              if (document.fonts && document.fonts.ready) await document.fonts.ready;
                              await new Promise((resolve) => {
                                requestAnimationFrame(() => requestAnimationFrame(resolve));
                              });
                            }"""
                        ),
                        timeout=3.5,
                    )
                except (asyncio.TimeoutError, PlaywrightError, PlaywrightTimeoutError):
                    pass
                try:
                    height_value = await asyncio.wait_for(
                        page.evaluate(
                            """() => {
                              const root = document.documentElement;
                              const body = document.body;
                              return Math.ceil(Math.max(
                                root ? root.scrollHeight : 0,
                                root ? root.offsetHeight : 0,
                                body ? body.scrollHeight : 0,
                                body ? body.offsetHeight : 0,
                                1
                              ));
                            }"""
                        ),
                        timeout=2.0,
                    )
                except (asyncio.TimeoutError, PlaywrightError, PlaywrightTimeoutError) as exc:
                    raise CaptureFailed("unable to determine rendered height") from exc
                if type(height_value) is not int:
                    raise CaptureFailed("renderer returned an invalid height")
                height = max(1, min(self._max_height, height_value))
                try:
                    image = await asyncio.wait_for(
                        page.screenshot(
                            type="png",
                            scale="css",
                            animations="disabled",
                            caret="hide",
                            full_page=False,
                            clip={"x": 0, "y": 0, "width": width, "height": height},
                        ),
                        timeout=8.0,
                    )
                except (asyncio.TimeoutError, PlaywrightError, PlaywrightTimeoutError) as exc:
                    raise CaptureFailed("renderer screenshot failed") from exc
                if type(image) is not bytes or not image.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise CaptureFailed("renderer returned a non-PNG image")
                if len(image) > self._max_bytes:
                    raise CaptureTooLarge("rendered image exceeds capture budget")
                return image
            except ArtifactCaptureError:
                raise
            except (PlaywrightError, PlaywrightTimeoutError, OSError, RuntimeError) as exc:
                raise CaptureFailed("renderer unavailable") from exc
            finally:
                for task in tuple(background_tasks):
                    if not task.done():
                        task.cancel()
                if background_tasks:
                    await asyncio.gather(*background_tasks, return_exceptions=True)
                await _close_quietly(page)
                await _close_quietly(context)
                await _close_quietly(browser)


__all__ = [
    "ArtifactCaptureError",
    "CaptureBusy",
    "CaptureClosed",
    "CaptureCoordinator",
    "CaptureFailed",
    "CaptureRenderer",
    "CaptureTimedOut",
    "CaptureTooLarge",
    "DEFAULT_CAPTURE_QUEUE",
    "DEFAULT_CAPTURE_WALL_TIME",
    "MAX_CAPTURE_QUEUE",
    "MAX_CAPTURE_WALL_TIME",
    "DEFAULT_CAPTURE_WIDTH",
    "MAX_CAPTURE_BYTES",
    "MAX_CAPTURE_HEIGHT",
    "MAX_CAPTURE_PROJECT_BYTES",
    "MAX_CAPTURE_WIDTH",
    "MIN_CAPTURE_WIDTH",
    "PlaywrightArtifactRenderer",
    "build_file_csp",
]
