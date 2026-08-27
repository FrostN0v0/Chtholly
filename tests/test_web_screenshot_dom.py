"""Behavioral tests for DOM screenshot section selection."""

from __future__ import annotations

import pytest
from playwright.async_api import Error as PlaywrightError, async_playwright

from plugins.llm_chat.web.screenshot_dom import locate_screenshot_region


@pytest.mark.asyncio
async def test_mediawiki_heading_wrapper_includes_following_section_content() -> None:
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(headless=True)
        except PlaywrightError as exc:
            if "Executable doesn't exist" in str(exc):
                pytest.skip("Playwright browser is not installed")
            raise
        try:
            page = await browser.new_page(viewport={"width": 1200, "height": 900})
            await page.set_content(
                """
                <style>
                  body { margin: 0; }
                  .mw-parser-output { width: 1100px; }
                  .mw-heading { width: 1080px; }
                  .skill-content { width: 1000px; height: 640px; background: #ddd; }
                  .later-content { width: 1000px; height: 7000px; }
                </style>
                <main class="mw-parser-output">
                  <div class="mw-heading mw-heading2"><h2 id="skills">技能</h2></div>
                  <p>技能1（精英0开放）</p>
                  <div class="skill-content">完整技能表</div>
                  <div class="mw-heading mw-heading2"><h2 id="base-skills">后勤技能</h2></div>
                  <div class="later-content">后续页面内容</div>
                </main>
                """
            )

            region = await locate_screenshot_region(page, "技能", 1200)
        finally:
            await browser.close()

    assert region is not None
    assert region.matched == "技能"
    assert region.width >= 1000
    assert 640 <= region.height < 1500
    assert not region.truncated
