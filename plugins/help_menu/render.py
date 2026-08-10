"""Rendering layer: MenuEntry groups -> PNG bytes via browser plugin."""

from pathlib import Path

from entari_plugin_browser import template2img

from .collect import MenuEntry

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


async def render_menu(
    grouped: dict[str, list[MenuEntry]],
    *,
    title: str,
    columns: int,
) -> bytes | None:
    total = sum(len(v) for v in grouped.values())
    return await template2img(
        template_path=str(TEMPLATE_DIR),
        template_name="menu.html.jinja",
        templates={
            "title": title,
            "grouped": grouped,
            "total": total,
            "columns": columns,
        },
        page_option={"viewport": {"width": 940, "height": 10}, "base_url": TEMPLATE_DIR.as_uri()},
        # quality=None overrides the renderer's jpeg default (quality=80),
        # which playwright rejects for png screenshots.
        screenshot_option={"type": "png", "full_page": True, "quality": None},
    )
