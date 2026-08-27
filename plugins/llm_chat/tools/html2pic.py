"""html2pic LLM tool implementation."""

from __future__ import annotations

from arclet.entari import Session
from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from ._rendering import DEFAULT_RENDER_WIDTH, RenderToolContext, render_and_deliver
from ..core.types import JSONType
from ._registration import register_tool
from ._render_policy import prepare_html_source


def register_html2pic(
    dispatcher: PluginDispatcher[JSONType],
    runtime: RenderToolContext,
) -> Subscriber[JSONType]:
    """Register bounded HTML-to-image delivery."""

    async def html2pic(session: Session, html: str, width: int = DEFAULT_RENDER_WIDTH) -> str:
        """Render one self-contained HTML/CSS document as an image and send it.

        Use this only when a custom visual layout, card, diagram, dashboard, or browser-style presentation is more
        useful than Markdown. Supply the full HTML and inline CSS. Put fixed dimensions and overflow clipping on an
        inner canvas instead of html/body; document-level height and overflow are normalized for full-page capture.
        Scripts, event handlers, iframes, navigation, remote or local resources, external fonts, and arbitrary file
        paths are rejected. This tool sends the image itself and consumes one media delivery; do not repeat it.

        Args:
            html (str): Complete self-contained HTML with optional inline CSS.
            width (int): Logical image width from 480 through 1200 pixels. Defaults to 900.
        Returns:
            str: Confirmed delivery status without echoing the HTML source.
        """

        prepared = prepare_html_source(html, max_chars=runtime.max_source_chars)

        async def render(renderer, raster, timeout):
            from entari_plugin_htmlrender import ResourceMaterializationPolicy

            return await renderer.rasterize_prepared(
                prepared,
                raster=raster,
                materialization_policy=ResourceMaterializationPolicy.OFF,
                timeout_seconds=timeout,
            )

        return await render_and_deliver(session, runtime, render, tool_name="html2pic", width=width)

    return register_tool(dispatcher, html2pic)
