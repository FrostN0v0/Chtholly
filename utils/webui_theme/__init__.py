"""Trusted local theme assets for opaque WebUI extension frames."""

from pathlib import Path

_ASSETS = Path(__file__).parent


def themed_assets(
    asset_dir: Path,
    *,
    script_names: tuple[str, ...] = ("app.js",),
    style_names: tuple[str, ...] = ("app.css",),
) -> tuple[str, str]:
    """Compose trusted local assets before the existing nonce/CSP boundary."""
    stylesheet = (_ASSETS / "tokens.css").read_text(encoding="utf-8")
    for name in style_names:
        stylesheet += "\n" + (asset_dir / name).read_text(encoding="utf-8")
    script = (_ASSETS / "frame-theme.js").read_text(encoding="utf-8")
    for name in script_names:
        script += "\n" + (asset_dir / name).read_text(encoding="utf-8")
    return stylesheet, script
