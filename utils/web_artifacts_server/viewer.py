"""Trusted viewer composition; static assets never contain generated project code."""

from __future__ import annotations

import json
import base64
from string import Template
import hashlib
from pathlib import Path
from collections.abc import Mapping

_ASSETS = Path(__file__).with_name("assets")
VIEWER_STYLE = (_ASSETS / "viewer.css").read_text(encoding="utf-8").strip()
VIEWER_SCRIPT = (_ASSETS / "viewer.js").read_text(encoding="utf-8").strip()
_VIEWER_TEMPLATE = Template((_ASSETS / "viewer.html").read_text(encoding="utf-8"))


def _csp_hash(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    return base64.b64encode(digest).decode("ascii")


_SCRIPT_HASH = _csp_hash(VIEWER_SCRIPT)
_STYLE_HASH = _csp_hash(VIEWER_STYLE)


def build_viewer_csp(public_origin: str, *, file_prefix: str) -> str:
    """Restrict framed navigation to this artifact, including script-initiated navigation."""

    if not file_prefix.startswith(public_origin + "/p/") or not file_prefix.endswith("/files/"):
        raise ValueError("viewer requires an artifact-scoped file prefix")
    return "; ".join(
        (
            "default-src 'none'",
            "base-uri 'none'",
            "object-src 'none'",
            "form-action 'none'",
            "frame-src " + file_prefix,
            "img-src 'self' data:",
            "font-src 'none'",
            "media-src 'none'",
            "connect-src 'none'",
            "worker-src 'none'",
            "child-src 'none'",
            "manifest-src 'none'",
            "script-src 'sha256-" + _SCRIPT_HASH + "'",
            "style-src 'sha256-" + _STYLE_HASH + "'",
            "frame-ancestors 'self'",
        )
    )


def build_viewer_document(config: Mapping[str, object]) -> bytes:
    """Substitute trusted assets once; metadata remains inert escaped JSON."""

    encoded = json.dumps(dict(config), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    safe_config = encoded.replace("&", r"\u0026").replace("<", r"\u003c").replace(">", r"\u003e")
    document = _VIEWER_TEMPLATE.substitute(
        config_json=safe_config,
        viewer_style_tag=f"<style>{VIEWER_STYLE}</style>",
        viewer_script_tag=f"<script>{VIEWER_SCRIPT}</script>",
    )
    return document.encode("utf-8")
