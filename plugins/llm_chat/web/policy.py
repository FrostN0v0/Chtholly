"""Provider-independent web access policy for llm_chat."""

from __future__ import annotations

from typing import Literal, TypedDict
import ipaddress
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from urllib.parse import urlsplit, parse_qsl, urlunsplit
from collections.abc import Iterator

URL_MAX_CHARS = 2048
_QUERY_MAX_CHARS = 500
_BLOCKED_HOSTS = {"localhost", "home.arpa"}
_BLOCKED_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".localdomain",
    ".home",
    ".home.arpa",
    ".lan",
    ".test",
    ".invalid",
    ".example",
    ".onion",
)
_SENSITIVE_QUERY_KEYS = {
    "token",
    "access_token",
    "api_key",
    "key",
    "secret",
    "password",
    "passwd",
    "credential",
    "credentials",
    "auth",
    "authorization",
    "signature",
    "sig",
    "session",
    "session_id",
    "cookie",
}
_SENSITIVE_QUERY_PREFIXES = ("x_amz_", "x_goog_", "oauth_")
WebToolName = Literal["web_search", "read_web_page"]


@dataclass(frozen=True)
class WebAccessLimits:
    search_limit: int
    read_limit: int
    total_limit: int


DEFAULT_WEB_ACCESS_LIMITS = WebAccessLimits(2, 2, 4)


def normalize_web_access_limits(
    search_limit: int,
    read_limit: int,
    total_limit: int,
) -> WebAccessLimits:
    """Return non-negative per-tool limits with a reachable total cap."""

    normalized_search = max(0, int(search_limit))
    normalized_read = max(0, int(read_limit))
    normalized_total = max(0, int(total_limit))
    return WebAccessLimits(
        search_limit=normalized_search,
        read_limit=normalized_read,
        total_limit=min(normalized_total, normalized_search + normalized_read),
    )


@dataclass
class WebAccessBudget:
    limits: WebAccessLimits
    search_calls: int = 0
    read_calls: int = 0
    total_calls: int = 0


_WEB_ACCESS_BUDGET: ContextVar[WebAccessBudget | None] = ContextVar(
    "llm_chat_web_access_budget",
    default=None,
)


class WebAccessError(RuntimeError):
    """A sanitized web access validation or provider error."""


class WebSearchItem(TypedDict):
    title: str
    url: str
    snippet: str


class WebSearchData(TypedDict):
    query: str
    results: list[WebSearchItem]


class WebPageData(TypedDict):
    url: str
    content: str


@contextmanager
def llm_chat_web_access_scope(
    limits: WebAccessLimits = DEFAULT_WEB_ACCESS_LIMITS,
) -> Iterator[None]:
    """Create an isolated budget for the current llm_chat generation."""

    token = _WEB_ACCESS_BUDGET.set(WebAccessBudget(limits))
    try:
        yield
    finally:
        _WEB_ACCESS_BUDGET.reset(token)


def require_llm_chat_web_access() -> None:
    """Reject web execution outside the llm_chat generation context."""

    if _WEB_ACCESS_BUDGET.get() is None:
        raise WebAccessError("Web access is unavailable outside llm_chat")


def consume_llm_chat_web_access(tool: WebToolName) -> None:
    """Consume one generation-local web call before any asynchronous work."""

    budget = _WEB_ACCESS_BUDGET.get()
    if budget is None:
        raise WebAccessError("Web access is unavailable outside llm_chat")
    if budget.total_calls >= budget.limits.total_limit:
        raise WebAccessError("Web access budget exhausted; answer from collected evidence without more web tools")
    if tool == "web_search":
        if budget.search_calls >= budget.limits.search_limit:
            raise WebAccessError("web_search budget exhausted; answer from collected evidence without more web tools")
        budget.search_calls += 1
    else:
        if budget.read_calls >= budget.limits.read_limit:
            raise WebAccessError(
                "read_web_page budget exhausted; answer from collected evidence without more web tools"
            )
        budget.read_calls += 1
    budget.total_calls += 1


def normalize_search_text(value: str, *, field: str) -> str:
    """Normalize a model-provided query or focus without exposing its value."""

    if not isinstance(value, str):
        raise WebAccessError(f"{field} is required")
    normalized = " ".join(value.split())
    if not normalized:
        raise WebAccessError(f"{field} is required")
    return _truncate(normalized, _QUERY_MAX_CHARS)


def normalize_public_url(url: str) -> str:
    """Return a canonical public HTTP(S) URL or reject it safely."""

    if not isinstance(url, str):
        raise WebAccessError("A valid public URL is required")
    candidate = url.strip()
    if not candidate or len(candidate) > URL_MAX_CHARS:
        raise WebAccessError("A valid public URL is required")

    try:
        parsed = urlsplit(candidate)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise WebAccessError("A valid public URL is required") from exc

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not hostname:
        raise WebAccessError("A valid public URL is required")
    if parsed.username is not None or parsed.password is not None:
        raise WebAccessError("A valid public URL is required")
    if hostname.endswith("."):
        raise WebAccessError("A valid public URL is required")

    try:
        ascii_host = hostname.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise WebAccessError("A valid public URL is required") from exc

    _validate_public_host(ascii_host)
    _validate_query_keys(parsed.query)

    if ":" in ascii_host:
        netloc = f"[{ascii_host}]"
    else:
        netloc = ascii_host
    if port is not None:
        netloc = f"{netloc}:{port}"

    normalized = urlunsplit((scheme, netloc, parsed.path, parsed.query, ""))
    if len(normalized) > URL_MAX_CHARS:
        raise WebAccessError("A valid public URL is required")
    return normalized


def _validate_public_host(host: str) -> None:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None

    if address is not None:
        if (
            not address.is_global
            or address.is_loopback
            or address.is_private
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            raise WebAccessError("A valid public URL is required")
        return

    labels = host.split(".")
    if any(not label for label in labels):
        raise WebAccessError("A valid public URL is required")
    if any(
        len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or not all(character.isalnum() or character == "-" for character in label)
        for label in labels
    ):
        raise WebAccessError("A valid public URL is required")
    if all(label.isdecimal() for label in labels):
        raise WebAccessError("A valid public URL is required")
    if len(labels) < 2 or host in _BLOCKED_HOSTS:
        raise WebAccessError("A valid public URL is required")
    if any(host.endswith(suffix) for suffix in _BLOCKED_HOST_SUFFIXES):
        raise WebAccessError("A valid public URL is required")


def _validate_query_keys(query: str) -> None:
    for key, _value in parse_qsl(query.replace(";", "&"), keep_blank_values=True):
        normalized = key.casefold().replace("-", "_")
        if normalized in _SENSITIVE_QUERY_KEYS or normalized.startswith(_SENSITIVE_QUERY_PREFIXES):
            raise WebAccessError("URLs containing sensitive query parameters are not allowed")


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    if limit <= 1:
        return "…"
    return f"{value[: limit - 1]}…"
