"""Trusted renderer setup and bounded image evidence; no candidate configuration."""

from __future__ import annotations

from io import BytesIO
import re
import base64
import hashlib
from pathlib import Path
import binascii
import tempfile
import warnings
from html.parser import HTMLParser

MAX_IMAGE_BYTES = 6 * 1024 * 1024
MAX_IMAGE_PIXELS = 16 * 1024 * 1024
MAX_DECODED_PIXELS = 32 * 1024 * 1024
MAX_IMAGE_FRAMES = 128
BROWSERS_PATH = "/tmp/workshop-playwright/browsers"
COMMAND_TIMEOUT = 30
_DATA_URI = re.compile(r"data:[^\s\"'<>]+", re.IGNORECASE)
_IMAGE_MIMES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}


class MediaValidationError(ValueError):
    """An emitted image could not be accepted as real, bounded pixels."""


def image_evidence(raw: bytes, mime: str) -> str:
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise MediaValidationError("Image is empty or exceeds 6 MiB")
    from PIL import Image, UnidentifiedImageError

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw)) as image:
                if image.format is None or _IMAGE_MIMES.get(image.format) != mime:
                    raise MediaValidationError("Image MIME does not match its decoded format")
                width, height = image.size
                if not 0 < width * height <= MAX_IMAGE_PIXELS:
                    raise MediaValidationError("Image exceeds the pixel limit")
                image.verify()
            with Image.open(BytesIO(raw)) as image:
                frames = 0
                pixels = 0
                while True:
                    frames += 1
                    pixels += image.width * image.height
                    if frames > MAX_IMAGE_FRAMES or pixels > MAX_DECODED_PIXELS:
                        raise MediaValidationError("Image animation exceeds the decode budget")
                    image.load()
                    try:
                        image.seek(frames)
                    except EOFError:
                        break
    except MediaValidationError:
        raise
    except (
        OSError,
        ValueError,
        SyntaxError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise MediaValidationError("Image pixel decoding failed") from exc
    return (
        f"[image mime={mime} width={width} height={height} bytes={len(raw)} "
        f"frames={frames} sha256={hashlib.sha256(raw).hexdigest()}]"
    )


def inline_image_evidence(uri: str) -> str:
    header, separator, encoded = uri.partition(",")
    if not separator or not header.lower().startswith("data:") or not header.lower().endswith(";base64"):
        raise MediaValidationError("Image must use an inline base64 data URI")
    mime = header[5:-7].lower()
    if mime not in _IMAGE_MIMES.values():
        raise MediaValidationError("Unsupported inline image MIME")
    if len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
        raise MediaValidationError("Inline image exceeds 6 MiB")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise MediaValidationError("Invalid image base64") from exc
    return image_evidence(raw, mime)


class _Images(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.sources: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() in {"img", "image"}:
            sources = [value for key, value in attrs if key == "src"]
            if len(sources) != 1 or not sources[0]:
                raise MediaValidationError("Image element must contain one source")
            if len(self.sources) >= 24:
                raise MediaValidationError("Message exceeds the image count limit")
            self.sources.append(sources[0])


def safe_effect(content: str) -> str:
    """Validate every image before text assertions, preserving captions and markup."""
    parser = _Images()
    parser.feed(content)
    parser.close()
    evidence = {source: inline_image_evidence(source) for source in parser.sources}
    for source, marker in evidence.items():
        content = content.replace(source, marker)
    # Data URIs in non-image effects must not leak binary payloads into the report.
    content = _DATA_URI.sub("[inline data omitted]", content)
    if len(content) > 8192:
        raise MediaValidationError("Visible command effect exceeds the evidence limit")
    return content


def prepare_renderer_environment():
    # HTMLRender writes its runtime-state JSON beside storage_path. Only this
    # parent is writable; the browser binaries remain in the read-only image.
    browsers = Path(BROWSERS_PATH)
    browsers.parent.mkdir(parents=True, exist_ok=True)
    browsers.symlink_to("/opt/workshop/browsers", target_is_directory=True)


def renderer_config(candidate_root: Path) -> dict:
    return {
        "provider": "playwright",
        "startup": "off",  # Trusted explicit warmup below owns capability failures.
        "provider_config": {
            "engine": "chromium",
            "storage_path": BROWSERS_PATH,
            "skip_browser_install": True,
            "close_on_exit": True,
            "local_local_resource_policy": "file",
        },
        "html": {
            "max_output_bytes": MAX_IMAGE_BYTES,
            "max_source_bytes": 1024 * 1024,
            "max_pixels": MAX_IMAGE_PIXELS,
            "max_concurrency": 1,
        },
        "resources": {
            "local_access": {"allow_any_path": False, "allowed_paths": [str(candidate_root)]},
            "cache": {"max_bytes": 16 * 1024 * 1024, "max_resource_bytes": MAX_IMAGE_BYTES},
            "remote_access": {"allow_private_networks": False, "max_redirects": 0},
        },
    }


async def verify_renderer(service, candidate_root: Path) -> str:
    from entari_plugin_htmlrender import TemplateRef, RasterOptions, ResourceMaterializationPolicy

    # No candidate source has been published. Exercise its exact authorized root
    # with a trusted temporary template, then remove it before candidate import.
    candidate_root.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".html", dir=candidate_root, encoding="utf-8", delete=False
    ) as template:
        template.write(
            "<!doctype html><html><head><style>html,body{margin:0;width:32px;height:24px;"
            "background:{{ color }}}</style></head><body></body></html>"
        )
        template_path = Path(template.name)
    try:
        image = await service.renderer.rasterize_template(
            TemplateRef(root=candidate_root, name=template_path.name),
            {"color": "#2468ac"},
            raster=RasterOptions(width=32, height=24, device_pixel_ratio=1, format="png"),
            materialization_policy=ResourceMaterializationPolicy.STRICT,
            timeout_seconds=COMMAND_TIMEOUT,
        )
        raw = bytes(image)
        marker = image_evidence(raw, "image/png")
        from PIL import Image

        with Image.open(BytesIO(raw)) as decoded:
            if decoded.size != (32, 24) or decoded.convert("RGB").getpixel((16, 12)) != (36, 104, 172):
                raise RuntimeError("Trusted renderer produced incorrect dimensions or pixels")
        return marker
    finally:
        template_path.unlink()
