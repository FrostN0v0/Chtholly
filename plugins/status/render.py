"""Render status snapshots as high-resolution PNG images."""

from __future__ import annotations

from pathlib import Path
from dataclasses import dataclass

from jinja2 import TemplateError
from playwright.async_api import Error as PlaywrightError
import entari_plugin_browser as browser  # entari: plugin

from utils.path import FONT_DIR
from utils.status_core import StatusSnapshot, format_rate, format_bytes, assess_health, format_duration

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
STATUS_FONT = FONT_DIR / "HYWenHei-85W.ttf"


class StatusRenderError(RuntimeError):
    """Raised when the browser status card cannot be rendered."""


@dataclass(frozen=True, slots=True)
class GaugeView:
    name: str
    percent: float
    detail: str
    tone: str


def _gauge_views(snapshot: StatusSnapshot) -> tuple[GaugeView, ...]:
    cpu_frequency = f"{snapshot.cpu.frequency_mhz / 1000:.2f} GHz" if snapshot.cpu.frequency_mhz else "Live sample"
    swap_detail = (
        f"{format_bytes(snapshot.swap.used_bytes)} / {format_bytes(snapshot.swap.total_bytes)}"
        if snapshot.swap.total_bytes
        else "Not configured"
    )
    return (
        GaugeView("CPU", snapshot.cpu.percent, cpu_frequency, "sky"),
        GaugeView(
            "Memory",
            snapshot.memory.percent,
            f"{format_bytes(snapshot.memory.used_bytes)} / {format_bytes(snapshot.memory.total_bytes)}",
            "rose",
        ),
        GaugeView("Swap", snapshot.swap.percent, swap_detail, "violet"),
        GaugeView(
            "Disk",
            snapshot.disk.percent,
            f"{format_bytes(snapshot.disk.used_bytes)} / {format_bytes(snapshot.disk.total_bytes)}",
            "mint",
        ),
    )


async def render_status(snapshot: StatusSnapshot, *, title: str, subtitle: str) -> bytes | None:
    try:
        return await browser.template2img(
            template_path=str(TEMPLATE_DIR),
            template_name="status.html.jinja",
            templates={
                "title": title,
                "subtitle": subtitle,
                "snapshot": snapshot,
                "gauges": _gauge_views(snapshot),
                "health": assess_health(snapshot),
                "font_url": STATUS_FONT.resolve().as_uri(),
                "generated_at": snapshot.generated_at.strftime("%Y-%m-%d %H:%M:%S %Z"),
            },
            filters={
                "bytes": format_bytes,
                "duration": format_duration,
                "rate": format_rate,
            },
            page_option={
                "viewport": {"width": 1000, "height": 10},
                "device_scale_factor": 1.5,
                "base_url": TEMPLATE_DIR.as_uri(),
            },
            screenshot_option={"type": "png", "full_page": True, "quality": None},
        )
    except (OSError, RuntimeError, TypeError, ValueError, TemplateError, PlaywrightError) as exc:
        raise StatusRenderError("Unable to render status image") from exc
