"""jinja2pic LLM tool implementation."""

from __future__ import annotations

from typing import cast

from arclet.entari import Session
from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from ._report import normalize_report_variables
from ._rendering import DEFAULT_RENDER_WIDTH, RenderToolContext, render_and_deliver
from ..core.types import JSONType
from ._registration import register_tool


def register_jinja2pic(
    dispatcher: PluginDispatcher[JSONType],
    runtime: RenderToolContext,
) -> Subscriber[JSONType]:
    """Register fixed-template Jinja report rendering."""

    async def jinja2pic(
        session: Session,
        title: str,
        subtitle: str = "",
        metrics: list[list[str]] = cast(list[list[str]], None),
        columns: list[str] = cast(list[str], None),
        rows: list[list[str]] = cast(list[list[str]], None),
        notes: list[str] = cast(list[str], None),
        width: int = DEFAULT_RENDER_WIDTH,
    ) -> str:
        """Render structured data through the built-in Jinja report template and send it as one image.

        Use this for a polished report containing summary metrics, a data table, and short notes. It uses one fixed,
        trusted template. Never pass Jinja source, template paths, HTML, or filesystem locations. Each metric is
        [label, value] or [label, value, detail]. Table columns and rows must be supplied together, and every row must
        match the column count. This tool sends the image itself and consumes one media delivery; do not repeat it.

        Args:
            title (str): Required report title.
            subtitle (str): Optional short context line below the title.
            metrics (list[list[str]]): Optional metrics, each with label, value, and optional detail.
            columns (list[str]): Optional table column labels, supplied together with rows.
            rows (list[list[str]]): Optional table rows with exactly one cell per column.
            notes (list[str]): Optional short bullet notes shown after the table.
            width (int): Logical image width from 480 through 1200 pixels. Defaults to 900.
        Returns:
            str: Confirmed delivery status without echoing the report data.
        """
        from entari_plugin_htmlrender import TemplateRef, ResourceMaterializationPolicy

        variables = normalize_report_variables(
            title=title,
            subtitle=subtitle,
            metrics=metrics,
            columns=columns,
            rows=rows,
            notes=notes,
            max_chars=runtime.max_source_chars,
        )
        template = TemplateRef(runtime.template_root, "report.html")

        async def render(renderer, raster, timeout):
            return await renderer.rasterize_template(
                template,
                variables,
                raster=raster,
                materialization_policy=ResourceMaterializationPolicy.OFF,
                timeout_seconds=timeout,
            )

        return await render_and_deliver(session, runtime, render, tool_name="jinja2pic", width=width)

    return register_tool(dispatcher, jinja2pic)
