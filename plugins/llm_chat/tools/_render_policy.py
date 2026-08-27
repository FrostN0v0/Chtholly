"""Safety policy for model-authored HTML and Markdown rendering."""

from __future__ import annotations

from typing import TYPE_CHECKING
from html.parser import HTMLParser

import markdown as markdown_lib

from ._rendering import MAX_RENDER_SOURCE_CHARS, DEFAULT_RENDER_FONT_FAMILY
from ..core.delivery import DeliveryError

if TYPE_CHECKING:
    from entari_plugin_htmlrender import PreparedHtml

_MARKDOWN_EXTENSIONS = (
    "pymdownx.tasklist",
    "tables",
    "fenced_code",
    "codehilite",
    "mdx_math",
    "pymdownx.tilde",
)
_SAFE_MATH_SCRIPT_TYPES = frozenset({"math/tex", "math/tex; mode=display"})
_BLOCKED_TAGS = frozenset({"applet", "base", "embed", "frame", "frameset", "iframe", "link", "object", "portal"})
_URL_ATTRIBUTES = frozenset(
    {
        "action",
        "background",
        "cite",
        "data",
        "formaction",
        "href",
        "longdesc",
        "manifest",
        "poster",
        "src",
        "srcset",
        "xlink:href",
    }
)
_SAFE_DATA_IMAGE_PREFIXES = (
    "data:image/gif;base64,",
    "data:image/jpeg;base64,",
    "data:image/png;base64,",
    "data:image/webp;base64,",
)
_DANGEROUS_CSS_TOKENS = (
    "@import",
    "behavior:",
    "expression(",
    "file:",
    "http:",
    "https:",
    "javascript:",
    "url(",
    "vbscript:",
    "-moz-binding",
)

_HTML_RENDER_STYLESHEET = f"""
html, body {{
    height: auto !important;
    min-height: 0 !important;
    max-height: none !important;
    overflow: visible !important;
    font-family: {DEFAULT_RENDER_FONT_FAMILY} !important;
}}
""".strip()

_MARKDOWN_FONT_STYLE = f"""
<style>
.markdown-body {{
    font-family: {DEFAULT_RENDER_FONT_FAMILY} !important;
}}
</style>
""".strip()


def _normalize_source(value: object, *, field: str, max_chars: int) -> str:
    if not isinstance(value, str):
        raise DeliveryError(f"{field} must be a string")
    normalized = value.strip()
    if not normalized:
        raise DeliveryError(f"{field} is required")
    if len(normalized) > max_chars:
        raise DeliveryError(f"{field} exceeds the configured character limit ({max_chars})")
    return normalized


def _safe_url_value(value: str) -> bool:
    compact = "".join(value.split()).casefold()
    if not compact or compact.startswith("#"):
        return True
    return compact.startswith(_SAFE_DATA_IMAGE_PREFIXES)


def _validate_css(css: str) -> str | None:
    folded = css.casefold()
    if "\\" in css:
        return "CSS escapes are not allowed"
    if any(token in folded for token in _DANGEROUS_CSS_TOKENS):
        return "CSS resource loading or active content is not allowed"
    return None


class _HtmlSafetyParser(HTMLParser):
    def __init__(self, *, allow_math_scripts: bool) -> None:
        super().__init__(convert_charrefs=True)
        self.allow_math_scripts = allow_math_scripts
        self.violation: str | None = None
        self._style_depth = 0

    def _reject(self, reason: str) -> None:
        if self.violation is None:
            self.violation = reason

    def _inspect_tag(self, tag: str, attrs: list[tuple[str, str | None]], *, start: bool) -> None:
        lowered_tag = tag.casefold()
        normalized_attrs = {name.casefold(): (value or "").strip() for name, value in attrs}

        if lowered_tag == "script":
            script_type = normalized_attrs.get("type", "").casefold()
            if not (
                self.allow_math_scripts and script_type in _SAFE_MATH_SCRIPT_TYPES and set(normalized_attrs) <= {"type"}
            ):
                self._reject("script elements are not allowed")
        elif lowered_tag in _BLOCKED_TAGS:
            self._reject(f"{lowered_tag} elements are not allowed")

        if lowered_tag == "meta" and normalized_attrs.get("http-equiv", "").casefold() == "refresh":
            self._reject("meta refresh is not allowed")

        for name, value in normalized_attrs.items():
            if name.startswith("on") or name == "srcdoc":
                self._reject("active HTML attributes are not allowed")
            elif name == "style":
                if reason := _validate_css(value):
                    self._reject(reason)
            elif name in _URL_ATTRIBUTES and not _safe_url_value(value):
                self._reject("external or local HTML resources are not allowed")

        if lowered_tag == "style" and start:
            self._style_depth += 1

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._inspect_tag(tag, attrs, start=True)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._inspect_tag(tag, attrs, start=False)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "style" and self._style_depth:
            self._style_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._style_depth and (reason := _validate_css(data)):
            self._reject(reason)


def _assert_safe_html(
    source: str,
    *,
    allow_math_scripts: bool,
    stylesheets: tuple[str, ...] = (),
) -> PreparedHtml:
    from entari_plugin_htmlrender import HtmlRenderError, DocumentRequirement, parse_html

    parser = _HtmlSafetyParser(allow_math_scripts=allow_math_scripts)
    try:
        parser.feed(source)
        parser.close()
    except (TypeError, ValueError) as exc:
        raise DeliveryError("HTML could not be parsed safely") from exc
    if parser.violation is not None:
        raise DeliveryError(parser.violation)

    try:
        prepared = parse_html(source, stylesheets=stylesheets)
    except HtmlRenderError as exc:
        raise DeliveryError("HTML could not be parsed safely") from exc

    blocked_requirements = set(prepared.requirements)
    if allow_math_scripts:
        blocked_requirements.discard(DocumentRequirement.JAVASCRIPT)
    if blocked_requirements:
        raise DeliveryError("external or local HTML resources are not allowed")
    if any(not _safe_url_value(reference) for reference in prepared.structure.references):
        raise DeliveryError("external or local HTML resources are not allowed")
    return prepared


def normalize_html_source(source: object, *, max_chars: int = MAX_RENDER_SOURCE_CHARS) -> str:
    """Validate model-authored HTML as a self-contained, scriptless document."""

    normalized = _normalize_source(source, field="html", max_chars=max_chars)
    _assert_safe_html(normalized, allow_math_scripts=False)
    return normalized


def prepare_html_source(source: object, *, max_chars: int = MAX_RENDER_SOURCE_CHARS) -> PreparedHtml:
    """Prepare safe HTML with document-level auto-height normalization."""

    normalized = _normalize_source(source, field="html", max_chars=max_chars)
    return _assert_safe_html(
        normalized,
        allow_math_scripts=False,
        stylesheets=(_HTML_RENDER_STYLESHEET,),
    )


def normalize_markdown_source(source: object, *, max_chars: int = MAX_RENDER_SOURCE_CHARS) -> str:
    """Validate Markdown after converting it with the renderer's extension set."""

    normalized = _normalize_source(source, field="markdown", max_chars=max_chars)
    try:
        rendered = markdown_lib.markdown(
            normalized,
            extensions=list(_MARKDOWN_EXTENSIONS),
            extension_configs={"mdx_math": {"enable_dollar_delimiter": True}},
        )
    except (TypeError, ValueError) as exc:
        raise DeliveryError("Markdown could not be parsed safely") from exc
    _assert_safe_html(rendered, allow_math_scripts=True)
    return normalized


def prepare_markdown_source(source: object, *, max_chars: int = MAX_RENDER_SOURCE_CHARS) -> str:
    """Prepare safe Markdown with the shared sans-serif font stack."""

    normalized = normalize_markdown_source(source, max_chars=max_chars)
    return f"{normalized}\n\n{_MARKDOWN_FONT_STYLE}"
