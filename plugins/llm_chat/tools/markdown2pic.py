"""markdown2pic LLM tool implementation."""

from __future__ import annotations

from arclet.entari import Session
from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from ._rendering import DEFAULT_RENDER_WIDTH, RenderToolContext, render_and_prepare
from ..core.types import JSONType
from ._registration import register_tool
from ._render_policy import prepare_markdown_source


def register_markdown2pic(
    dispatcher: PluginDispatcher[JSONType],
    runtime: RenderToolContext,
) -> Subscriber[JSONType]:
    """Register bounded Markdown-to-image preparation."""

    async def markdown2pic(session: Session, markdown: str, width: int = DEFAULT_RENDER_WIDTH) -> dict[str, JSONType]:
        """Render self-contained Markdown and prepare one image for send_msg.

        Use this for fenced code blocks, configuration examples, Markdown tables, multi-column comparisons, long
        structured reports, or mixed headings and code. Include the returned media_ref in a send_msg media segment;
        choose the order and spacing of any accompanying explanation explicitly. Do not repeat rendered content.
        Keep code as text only when the user explicitly needs copyable source or the snippet is at most three short
        lines. The image uses Inter with Noto Sans SC/CJK SC for Chinese fallback. The Markdown must not embed
        scripts, event handlers, remote or local images, stylesheets, or other resources. This tool does not send.

        Args:
            markdown (str): Complete Markdown source to render, including any table syntax.
            width (int): Logical image width from 480 through 1200 pixels. Defaults to 900.
        Returns:
            dict[str, JSONType]: Prepared image resource containing a media_ref for send_msg.
        """

        source = prepare_markdown_source(markdown, max_chars=runtime.max_source_chars)

        async def render(renderer, raster, timeout):
            from entari_plugin_htmlrender import ResourceMaterializationPolicy

            return await renderer.rasterize_markdown(
                source,
                raster=raster,
                materialization_policy=ResourceMaterializationPolicy.OFF,
                timeout_seconds=timeout,
            )

        return await render_and_prepare(session, runtime, render, tool_name="markdown2pic", width=width)

    return register_tool(dispatcher, markdown2pic)
