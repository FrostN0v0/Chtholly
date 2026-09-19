"""Smoke-only Entari route proving both prepared renderer services produce pixels."""

from io import BytesIO
from typing import cast
from pathlib import Path

from PIL import Image
from launart import Launart
from arclet.entari import plugin
from starlette.responses import JSONResponse
import entari_plugin_server as server_plugin  # entari: plugin
from entari_plugin_browser import playwright_api  # entari: plugin
from starlette.applications import Starlette
from entari_plugin_htmlrender import TemplateRef, RasterOptions
from entari_plugin_htmlrender.entari.service import HtmlRenderService

_HTML = (
    "<!doctype html><html><head><style>html,body{margin:0;width:32px;height:24px;"
    "background:#2468ac}</style></head><body></body></html>"
)


def valid_image(raw: bytes) -> bool:
    with Image.open(BytesIO(raw)) as image:
        image.load()
        return image.size == (32, 24) and image.convert("RGB").getpixel((16, 12)) == (36, 104, 172)


@server_plugin.server.asgi_route("/__startup_smoke__/render")
async def render_probe(request):
    try:
        service = cast(HtmlRenderService, Launart.current().get_component("htmlrender.runtime"))
        rendered = await service.renderer.rasterize_template(
            TemplateRef(root=Path.cwd() / "smoke-templates", name="sample.html"),
            {},
            raster=RasterOptions(width=32, height=24, device_pixel_ratio=1, format="png"),
            timeout_seconds=15,
        )
        legacy = None
        async with playwright_api.page(viewport={"width": 32, "height": 24}) as page:
            await page.set_content(_HTML)
            legacy = await page.screenshot(type="png")
        passed = legacy is not None and valid_image(legacy) and valid_image(bytes(rendered))
        return JSONResponse({"browser": passed, "htmlrender": passed}, status_code=200 if passed else 503)
    except Exception as error:
        return JSONResponse({"not_ready": type(error).__name__}, status_code=503)


_application = cast(Starlette, server_plugin.server.app)
_route = _application.routes[-1]


def cleanup() -> None:
    if _route in _application.routes:
        _application.routes.remove(_route)


plugin.collect_disposes(cleanup)
