"""DOM selection and geometry for bounded webpage screenshots."""

from __future__ import annotations

import math
from collections.abc import Mapping

from playwright.async_api import Page, Error as PlaywrightError

from .screenshot_models import ScreenshotRegion, WebScreenshotError

SECTION_MAX_HEIGHT = 5000
SECTION_MIN_HEIGHT = 320
OVERVIEW_MAX_HEIGHT = 2400

_REGION_SCRIPT = r"""
({ focus, maxHeight, minSectionHeight, overviewHeight, maxWidth }) => {
    const normalize = (value) => String(value || "").replace(/\s+/g, " ").trim().toLocaleLowerCase();
    const focusText = normalize(focus);
    const headingSelector = "h1,h2,h3,h4,h5,h6,[role=heading],summary,caption,dt,.mw-headline";
    const visible = (element) => {
        if (!(element instanceof Element)) return false;
        const style = getComputedStyle(element);
        const rect = element.getBoundingClientRect();
        return style.display !== "none" && style.visibility !== "hidden" && Number(style.opacity) !== 0
            && rect.width > 1 && rect.height > 1;
    };
    const score = (element, heading) => {
        if (!visible(element)) return null;
        const text = normalize(element.textContent);
        if (!text) return null;
        let value = null;
        if (text === focusText) value = 0;
        else if (text.includes(focusText)) value = 10 + Math.min(text.length, 100) / 100;
        else if (text.length >= 2 && focusText.includes(text)) {
            const trailing = focusText.length - focusText.lastIndexOf(text) - text.length;
            value = 20 + Math.max(0, trailing) / 1000 + Math.min(text.length, 100) / 100000;
        }
        if (value === null) return null;
        return value + (heading ? 0 : 100);
    };
    const choose = (elements, heading) => {
        let selected = null;
        let selectedScore = Infinity;
        for (const element of elements) {
            const current = score(element, heading);
            if (current !== null && current < selectedScore) {
                selected = element;
                selectedScore = current;
            }
        }
        return selected;
    };
    const headingElement = (element) => {
        if (element.matches("h1,h2,h3,h4,h5,h6")) return element;
        const ancestor = element.closest("h1,h2,h3,h4,h5,h6");
        if (ancestor) return ancestor;
        return element.querySelector(":scope > h1,:scope > h2,:scope > h3,:scope > h4,:scope > h5,:scope > h6");
    };
    const headingLevel = (element) => {
        const heading = headingElement(element);
        return heading ? Number(heading.tagName.slice(1)) : null;
    };
    const rectangles = [];
    const addRect = (element) => {
        if (!visible(element)) return;
        const rect = element.getBoundingClientRect();
        rectangles.push({
            left: rect.left + scrollX,
            top: rect.top + scrollY,
            right: rect.right + scrollX,
            bottom: rect.bottom + scrollY,
        });
    };

    let candidate = null;
    if (focusText) {
        candidate = choose([...document.querySelectorAll(headingSelector)], true);
        if (!candidate) {
            const content = [...document.querySelectorAll("p,th,td,li,div,span")].slice(0, 8000);
            candidate = choose(content, false);
        }
        if (!candidate) return null;
    }

    let matched = null;
    if (candidate) {
        matched = String(candidate.textContent || "").replace(/\s+/g, " ").trim().slice(0, 160);
        const heading = candidate.matches("h1,h2,h3,h4,h5,h6")
            ? candidate
            : candidate.closest("h1,h2,h3,h4,h5,h6");
        if (heading) {
            const level = headingLevel(heading);
            let sectionAnchor = heading;
            while (sectionAnchor.parentElement && sectionAnchor.parentElement !== document.body) {
                const parent = sectionAnchor.parentElement;
                if (normalize(parent.textContent) !== normalize(heading.textContent)) break;
                sectionAnchor = parent;
            }
            let node = sectionAnchor;
            let count = 0;
            while (node && count < 120) {
                if (node !== sectionAnchor) {
                    const nextLevel = headingLevel(node);
                    if (nextLevel !== null && level !== null && nextLevel <= level) break;
                }
                addRect(node);
                node = node.nextElementSibling;
                count += 1;
            }
            const selectedTop = Math.min(...rectangles.map((rect) => rect.top));
            const selectedBottom = Math.max(...rectangles.map((rect) => rect.bottom));
            if (selectedBottom - selectedTop < minSectionHeight) {
                let container = sectionAnchor.parentElement;
                let selectedContainer = null;
                let selectedHeight = 0;
                while (container && container !== document.body) {
                    const rect = container.getBoundingClientRect();
                    if (visible(container) && rect.height >= minSectionHeight) {
                        if (rect.height > maxHeight * 1.25) break;
                        if (selectedContainer && rect.height > selectedHeight * 1.25) break;
                        selectedContainer = container;
                        selectedHeight = rect.height;
                    }
                    container = container.parentElement;
                }
                if (selectedContainer) addRect(selectedContainer);
            }
        } else {
            let container = candidate.closest("table,section,article,details,figure") || candidate;
            let parent = container.parentElement;
            while (parent && parent !== document.body) {
                const rect = parent.getBoundingClientRect();
                if (parent.matches("table,section,article,details,figure") && rect.height <= maxHeight * 1.25) {
                    container = parent;
                    parent = parent.parentElement;
                    continue;
                }
                break;
            }
            addRect(container);
        }
    } else {
        const overview = document.querySelector("main,article,#content,.mw-parser-output") || document.body;
        addRect(overview);
    }

    if (!rectangles.length) return null;
    const padding = 20;
    const documentElement = document.documentElement;
    const body = document.body;
    const documentWidth = Math.max(documentElement.scrollWidth, body?.scrollWidth || 0, innerWidth);
    const documentHeight = Math.max(documentElement.scrollHeight, body?.scrollHeight || 0, innerHeight);
    const left = Math.min(...rectangles.map((rect) => rect.left));
    const top = Math.min(...rectangles.map((rect) => rect.top));
    const right = Math.max(...rectangles.map((rect) => rect.right));
    const bottom = Math.max(...rectangles.map((rect) => rect.bottom));
    let x = Math.max(0, left - padding);
    const y = Math.max(0, top - padding);
    const naturalWidth = Math.max(1, Math.min(documentWidth, right + padding) - x);
    const naturalHeight = Math.max(1, Math.min(documentHeight, bottom + padding) - y);
    const width = Math.max(1, Math.min(naturalWidth, maxWidth));
    if (x + width > documentWidth) x = Math.max(0, documentWidth - width);
    const heightLimit = focusText ? maxHeight : overviewHeight;
    const height = Math.max(1, Math.min(naturalHeight, heightLimit, documentHeight - y));
    return {
        clip: { x, y, width, height },
        matched,
        truncated: naturalHeight > height,
    };
}
"""

_SETTLE_SCRIPT = r"""
async ({ timeoutMs }) => {
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    if (document.fonts?.ready) await document.fonts.ready;
    const pending = [...document.images]
        .filter((image) => !image.complete)
        .slice(0, 80)
        .map((image) => new Promise((resolve) => {
            image.addEventListener("load", resolve, { once: true });
            image.addEventListener("error", resolve, { once: true });
        }));
    await Promise.race([
        Promise.all(pending),
        new Promise((resolve) => setTimeout(resolve, timeoutMs)),
    ]);
}
"""

_MATERIALIZE_IMAGES_SCRIPT = r"""
({ x, y, width, height }) => {
    const right = x + width;
    const bottom = y + height;
    const publicSource = (value) => {
        if (!value) return null;
        try {
            const url = new URL(value, document.baseURI);
            return url.protocol === "http:" || url.protocol === "https:" ? url.href : null;
        } catch {
            return null;
        }
    };
    let changed = 0;
    for (const image of document.images) {
        const rect = image.getBoundingClientRect();
        if (rect.right <= x || rect.left >= right || rect.bottom <= y || rect.top >= bottom) continue;
        if (image.loading === "lazy") {
            image.loading = "eager";
            changed += 1;
        }
        const lazySource = image.getAttribute("data-src")
            || image.getAttribute("data-original")
            || image.getAttribute("data-lazy-src");
        const source = publicSource(lazySource);
        if (source && image.src !== source) {
            image.src = source;
            changed += 1;
        }
    }
    return changed;
}
"""

_HIDE_FIXED_SCRIPT = r"""
async () => {
    const hideFixed = () => {
        for (const element of document.body.querySelectorAll("*")) {
            if (element.dataset.llmScreenshotOverlay === "hidden") continue;
            if (getComputedStyle(element).position !== "fixed") continue;
            const rect = element.getBoundingClientRect();
            if (rect.width <= 1 || rect.height <= 1) continue;
            element.dataset.llmScreenshotOverlay = "hidden";
            element.style.setProperty("display", "none", "important");
        }
    };
    hideFixed();
    if (!window.__llmScreenshotObserver) {
        window.__llmScreenshotObserver = new MutationObserver(hideFixed);
        window.__llmScreenshotObserver.observe(document.body, { childList: true, subtree: true });
    }
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    hideFixed();
}
"""


def _number(value: object, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WebScreenshotError(f"browser returned an invalid {field}")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise WebScreenshotError(f"browser returned an invalid {field}")
    return normalized


def _parse_region(value: object) -> ScreenshotRegion | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise WebScreenshotError("browser returned an invalid screenshot region")
    clip = value.get("clip")
    if not isinstance(clip, Mapping):
        raise WebScreenshotError("browser returned an invalid screenshot region")
    width = _number(clip.get("width"), field="region width")
    height = _number(clip.get("height"), field="region height")
    if width <= 0 or height <= 0:
        raise WebScreenshotError("browser returned an empty screenshot region")
    matched = value.get("matched")
    return ScreenshotRegion(
        x=max(0.0, _number(clip.get("x"), field="region x")),
        y=max(0.0, _number(clip.get("y"), field="region y")),
        width=width,
        height=height,
        matched=matched if isinstance(matched, str) and matched else None,
        truncated=value.get("truncated") is True,
    )


async def settle_page(page: Page, timeout_ms: int = 1500) -> None:
    try:
        await page.evaluate(_SETTLE_SCRIPT, {"timeoutMs": timeout_ms})
    except PlaywrightError:
        return


async def materialize_screenshot_images(page: Page, region: ScreenshotRegion) -> int:
    try:
        changed = await page.evaluate(
            _MATERIALIZE_IMAGES_SCRIPT,
            {"x": region.x, "y": region.y, "width": region.width, "height": region.height},
        )
    except PlaywrightError as exc:
        raise WebScreenshotError("browser failed to prepare screenshot images") from exc
    if type(changed) is not int or changed < 0:
        raise WebScreenshotError("browser returned an invalid prepared image count")
    return changed


async def locate_screenshot_region(page: Page, focus: str, max_width: int) -> ScreenshotRegion | None:
    value = await page.evaluate(
        _REGION_SCRIPT,
        {
            "focus": focus,
            "maxHeight": SECTION_MAX_HEIGHT,
            "minSectionHeight": SECTION_MIN_HEIGHT,
            "overviewHeight": OVERVIEW_MAX_HEIGHT,
            "maxWidth": max_width,
        },
    )
    return _parse_region(value)


async def prepare_screenshot_region(page: Page, focus: str, viewport_width: int) -> ScreenshotRegion | None:
    """Resize and scroll so the document region becomes a viewport-relative clip."""

    region = await locate_screenshot_region(page, focus, viewport_width)
    if region is None:
        return None
    viewport_height = max(1, int(math.ceil(region.height)) + 40)
    await page.set_viewport_size({"width": viewport_width, "height": viewport_height})
    region = await locate_screenshot_region(page, focus, viewport_width)
    if region is None:
        return None
    await page.evaluate(
        "([x, y]) => window.scrollTo(x, y)",
        [max(0.0, region.x - 20), max(0.0, region.y - 20)],
    )
    scroll = await page.evaluate("() => ({x: window.scrollX, y: window.scrollY})")
    if not isinstance(scroll, Mapping):
        raise WebScreenshotError("browser returned an invalid scroll position")
    scroll_x = _number(scroll.get("x"), field="scroll x")
    scroll_y = _number(scroll.get("y"), field="scroll y")
    x = max(0.0, region.x - scroll_x)
    y = max(0.0, region.y - scroll_y)
    width = min(region.width, max(0.0, viewport_width - x))
    height = min(region.height, max(0.0, viewport_height - y))
    if width <= 0 or height <= 0:
        raise WebScreenshotError("browser returned an out-of-view screenshot region")
    return ScreenshotRegion(x, y, width, height, region.matched, region.truncated)


async def hide_fixed_elements(page: Page) -> None:
    try:
        await page.evaluate(_HIDE_FIXED_SCRIPT)
    except PlaywrightError:
        return
