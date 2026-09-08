"""Standalone aiohttp server for expiring web-artifact previews."""

from __future__ import annotations

import os
import re
import hmac
import json
import time
import asyncio
from pathlib import Path
import argparse
import ipaddress
from urllib.parse import quote, unquote, urlsplit
from collections.abc import Mapping, Callable, Sequence, Awaitable

from aiohttp import web

from utils.web_artifacts_core import ArtifactStore

from .viewer import build_viewer_csp, build_viewer_document
from .capture import (
    MAX_CAPTURE_BYTES,
    MAX_CAPTURE_WIDTH,
    MIN_CAPTURE_WIDTH,
    DEFAULT_CAPTURE_QUEUE,
    DEFAULT_CAPTURE_WIDTH,
    DEFAULT_CAPTURE_WALL_TIME,
    CaptureBusy,
    CaptureClosed,
    CaptureFailed,
    CaptureRenderer,
    CaptureTimedOut,
    CaptureTooLarge,
    CaptureCoordinator,
    PlaywrightArtifactRenderer,
    build_file_csp,
)

MAX_CAPTURE_BODY_BYTES = 16 * 1024
MAX_ZIP_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_PREVIEW_RESPONSE_BYTES = MAX_CAPTURE_BYTES
TOKEN_RE = re.compile(r"^[A-Za-z0-9._~-]{1,256}$")

_NOT_FOUND_BODY = (
    b'<!doctype html><html lang="en"><head><meta charset="utf-8">'
    b'<meta name="viewport" content="width=device-width,initial-scale=1">'
    b"<title>Preview unavailable</title></head><body>"
    b"<main><h1>Preview unavailable</h1><p>This preview is unavailable or has expired.</p></main>"
    b"</body></html>"
)
_RATE_LIMIT_BODY = b"Too many requests."


class BoundedRateLimiter:
    """A bounded per-client token bucket with deterministic eviction."""

    def __init__(
        self,
        *,
        max_entries: int = 1024,
        rate_per_second: float = 12.0,
        burst: int = 48,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be positive")
        if burst <= 0:
            raise ValueError("burst must be positive")
        if not callable(clock):
            raise ValueError("clock must be callable")
        self.max_entries = int(max_entries)
        self.rate_per_second = float(rate_per_second)
        self.burst = int(burst)
        self.clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}

    @property
    def size(self) -> int:
        return len(self._buckets)

    def _evict(self, now: float) -> None:
        expiry = max(1.0, self.burst / self.rate_per_second * 2.0)
        stale = [key for key, (_, updated) in self._buckets.items() if now - updated > expiry]
        for key in stale:
            self._buckets.pop(key, None)
        while len(self._buckets) >= self.max_entries:
            oldest = min(self._buckets, key=lambda key: self._buckets[key][1])
            self._buckets.pop(oldest, None)

    def allow(self, key: str) -> bool:
        now = float(self.clock())
        bucket = self._buckets.get(key)
        if bucket is None:
            self._evict(now)
            self._buckets[key] = (float(self.burst - 1), now)
            return True
        tokens, updated = bucket
        tokens = min(float(self.burst), tokens + max(0.0, now - updated) * self.rate_per_second)
        if tokens < 1.0:
            self._buckets[key] = (tokens, now)
            return False
        self._buckets[key] = (tokens - 1.0, now)
        return True


def _normalize_public_origin(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("public_origin must be a non-empty HTTPS origin")
    parsed = urlsplit(value)
    if parsed.scheme.casefold() != "https" or parsed.username or parsed.password:
        raise ValueError("public_origin must be an HTTPS origin")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("public_origin must not contain a path or query")
    host = parsed.hostname
    if not host:
        raise ValueError("public_origin must include a host")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("public_origin has an invalid port") from exc
    host_text = f"[{host}]" if ":" in host else host
    return f"https://{host_text.lower()}" + (f":{port}" if port is not None and port != 443 else "")


def _valid_token(token: str) -> bool:
    return bool(TOKEN_RE.fullmatch(token))


def _quote_token(token: str) -> str:
    return quote(token, safe="")


def _quote_file_path(path: str) -> str:
    return quote(path, safe="/")


def _decode_file_path(value: str) -> str | None:
    if not isinstance(value, str):
        return None
    path = unquote(value)
    if not path or path.startswith(("/", "\\")) or "\\" in path or "\x00" in path:
        return None
    if any(part in ("", ".", "..") for part in path.split("/")):
        return None
    if any(ord(character) < 32 for character in path):
        return None
    return path


def _client_key(request: web.Request) -> str:
    if _is_loopback(request):
        forwarded = request.headers.get("X-Artifact-Client-IP", "")
        if forwarded:
            try:
                return str(ipaddress.ip_address(forwarded))
            except ValueError:
                pass
    remote = request.remote
    if remote:
        return remote
    transport = request.transport
    if transport is not None:
        peer = transport.get_extra_info("peername")
        if isinstance(peer, tuple) and peer:
            return str(peer[0])
        if peer:
            return str(peer)
    return "unknown"


def _is_loopback(request: web.Request) -> bool:
    remote = request.remote
    if not remote and request.transport is not None:
        peer = request.transport.get_extra_info("peername")
        if isinstance(peer, tuple):
            remote = str(peer[0]) if peer else None
        elif peer:
            remote = str(peer)
    if not remote:
        return False
    try:
        return ipaddress.ip_address(remote).is_loopback
    except ValueError:
        return False


def _base_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Robots-Tag": "noindex, nofollow, noarchive",
        "X-DNS-Prefetch-Control": "off",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    }


def _not_found_response() -> web.Response:
    headers = _base_headers()
    headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
    headers["Content-Type"] = "text/html; charset=utf-8"
    return web.Response(status=404, body=_NOT_FOUND_BODY, headers=headers)


def _rate_limited_response() -> web.Response:
    headers = _base_headers()
    headers["Retry-After"] = "1"
    headers["Content-Type"] = "text/plain; charset=utf-8"
    return web.Response(status=429, body=_RATE_LIMIT_BODY, headers=headers)


def _private_json(payload: Mapping[str, object], *, status: int) -> web.Response:
    encoded = json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    headers = _base_headers()
    headers["Content-Type"] = "application/json; charset=utf-8"
    headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
    return web.Response(status=status, body=encoded, headers=headers)


def _artifact_config(public_origin: str, token: str, artifact: object) -> dict[str, object]:
    encoded_token = _quote_token(token)
    artifact_prefix = f"/p/{encoded_token}/"
    file_prefix = f"{artifact_prefix}files/"
    entry = str(getattr(artifact, "entry"))
    files = tuple(getattr(artifact, "files"))
    return {
        "title": str(getattr(artifact, "title")),
        "version": int(getattr(artifact, "version")),
        "entry": entry,
        "expires_at": float(getattr(artifact, "expires_at")),
        "file_count": len(files),
        "source_bytes": int(getattr(artifact, "source_bytes")),
        "artifact_prefix": artifact_prefix,
        "file_prefix": file_prefix,
        "entry_url": file_prefix + _quote_file_path(entry),
        "source_url": artifact_prefix + "source.zip",
        "viewer_origin": public_origin,
    }


def _public_meta(artifact: object) -> dict[str, object]:
    files_payload: list[dict[str, object]] = []
    for info in tuple(getattr(artifact, "files")):
        files_payload.append(
            {
                "path": str(getattr(info, "path")),
                "mime": str(getattr(info, "mime")),
                "size": int(getattr(info, "size")),
            }
        )
    return {
        "title": str(getattr(artifact, "title")),
        "version": int(getattr(artifact, "version")),
        "entry": str(getattr(artifact, "entry")),
        "expires_at": float(getattr(artifact, "expires_at")),
        "files": files_payload,
        "source_bytes": int(getattr(artifact, "source_bytes")),
        "zip_bytes": int(getattr(artifact, "zip_bytes")),
    }


@web.middleware
async def _uniform_public_404(
    request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
) -> web.StreamResponse:
    if not request.path.startswith("/p/"):
        return await handler(request)
    try:
        response = await handler(request)
    except web.HTTPNotFound:
        return _not_found_response()
    if response.status == 404:
        return _not_found_response()
    return response


def create_app(
    store: ArtifactStore,
    *,
    public_origin: str,
    capture_token: str,
    capture_enabled: bool = True,
    capture_renderer: CaptureRenderer | Callable[[str, int], Awaitable[bytes]] | None = None,
    rate_limiter: BoundedRateLimiter | None = None,
    capture_max_queue: int = DEFAULT_CAPTURE_QUEUE,
    capture_wall_time: float = DEFAULT_CAPTURE_WALL_TIME,
) -> web.Application:
    """Create the read-only public preview application."""

    if getattr(store, "read_only", True) is False:
        raise ValueError("preview server requires a read-only artifact store")
    origin = _normalize_public_origin(public_origin)
    if capture_enabled and not isinstance(capture_token, str):
        raise ValueError("capture_token must be text")
    if capture_enabled and not capture_token:
        raise ValueError("capture_token is required when capture is enabled")

    limiter = rate_limiter or BoundedRateLimiter()
    renderer: CaptureRenderer | Callable[[str, int], Awaitable[bytes]] | None = None
    coordinator: CaptureCoordinator | None = None
    if capture_enabled:
        renderer = capture_renderer or PlaywrightArtifactRenderer(store)
        coordinator = CaptureCoordinator(renderer, max_queue=capture_max_queue, wall_time=capture_wall_time)

    app = web.Application(client_max_size=MAX_CAPTURE_BODY_BYTES, middlewares=[_uniform_public_404])

    async def startup(_app: web.Application) -> None:
        await asyncio.to_thread(store.initialize)

    async def cleanup(_app: web.Application) -> None:
        if coordinator is not None:
            await coordinator.close()
        await asyncio.to_thread(store.close)

    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)

    def public_gate(request: web.Request) -> web.Response | None:
        if not limiter.allow(_client_key(request)):
            return _rate_limited_response()
        return None

    async def health(request: web.Request) -> web.Response:
        try:
            await asyncio.to_thread(store.initialize)
        except Exception:
            return _private_json({"status": "unavailable"}, status=503)
        return _private_json({"status": "ok"}, status=200)

    async def viewer(request: web.Request) -> web.Response:
        limited = public_gate(request)
        if limited is not None:
            return limited
        token = request.match_info.get("token", "")
        if not _valid_token(token):
            return _not_found_response()
        try:
            artifact = await asyncio.to_thread(store.get_public, token)
            config = _artifact_config(origin, token, artifact)
            body = build_viewer_document(config)
        except Exception:
            return _not_found_response()
        encoded_token = _quote_token(token)
        file_prefix = f"{origin}/p/{encoded_token}/files/"
        headers = _base_headers()
        headers["Content-Type"] = "text/html; charset=utf-8"
        headers["Content-Security-Policy"] = build_viewer_csp(origin, file_prefix=file_prefix)
        headers["Cross-Origin-Opener-Policy"] = "same-origin"
        headers["Cross-Origin-Resource-Policy"] = "same-origin"
        headers["X-Frame-Options"] = "SAMEORIGIN"
        return web.Response(status=200, body=body, headers=headers)

    async def artifact_file(request: web.Request) -> web.Response:
        limited = public_gate(request)
        if limited is not None:
            return limited
        token = request.match_info.get("token", "")
        path = _decode_file_path(request.match_info.get("path", ""))
        if not _valid_token(token) or path is None:
            return _not_found_response()
        try:
            artifact = await asyncio.to_thread(store.get_public, token)
            registered_info = next(
                (info for info in tuple(getattr(artifact, "files")) if str(getattr(info, "path")) == path),
                None,
            )
            if registered_info is None:
                return _not_found_response()
            registered_mime = str(getattr(registered_info, "mime"))
            if request.headers.get("Sec-Fetch-Dest", "").casefold() == "document" and registered_mime in {
                "text/html",
                "image/svg+xml",
            }:
                headers = _base_headers()
                headers["Content-Security-Policy"] = build_viewer_csp(
                    origin,
                    file_prefix=f"{origin}/p/{_quote_token(token)}/files/",
                )
                headers["Location"] = f"/p/{_quote_token(token)}/"
                headers["Content-Type"] = "text/plain; charset=utf-8"
                return web.Response(status=302, body=b"", headers=headers)
            data, mime = await asyncio.to_thread(store.read_public_file, token, path)
            if type(data) is not bytes or len(data) > MAX_ZIP_RESPONSE_BYTES:
                return _not_found_response()
        except Exception:
            return _not_found_response()
        encoded_token = _quote_token(token)
        headers = _base_headers()
        headers["Content-Type"] = str(mime)
        headers["Content-Security-Policy"] = build_file_csp(
            f"{origin}/p/{encoded_token}/files/",
            origin,
        )
        headers["Access-Control-Allow-Origin"] = "*"
        headers["Cross-Origin-Resource-Policy"] = "cross-origin"
        headers["Cross-Origin-Opener-Policy"] = "same-origin"
        headers["X-Frame-Options"] = "SAMEORIGIN"
        return web.Response(status=200, body=data, headers=headers)

    async def source_zip(request: web.Request) -> web.Response:
        limited = public_gate(request)
        if limited is not None:
            return limited
        token = request.match_info.get("token", "")
        if not _valid_token(token):
            return _not_found_response()
        try:
            artifact = await asyncio.to_thread(store.get_public, token)
            data = await asyncio.to_thread(store.zip_public, token)
            if type(data) is not bytes or len(data) > MAX_ZIP_RESPONSE_BYTES:
                return _not_found_response()
            version = int(getattr(artifact, "version"))
        except Exception:
            return _not_found_response()
        headers = _base_headers()
        headers["Content-Type"] = "application/zip"
        headers["Content-Disposition"] = f'attachment; filename="source-v{version}.zip"'
        headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        headers["Cross-Origin-Resource-Policy"] = "same-origin"
        return web.Response(status=200, body=data, headers=headers)

    async def preview_png(request: web.Request) -> web.Response:
        limited = public_gate(request)
        if limited is not None:
            return limited
        token = request.match_info.get("token", "")
        if not _valid_token(token):
            return _not_found_response()
        try:
            await asyncio.to_thread(store.get_public, token)
            data = await asyncio.to_thread(store.preview_public, token)
            if (
                type(data) is not bytes
                or not data.startswith(b"\x89PNG\r\n\x1a\n")
                or len(data) > MAX_PREVIEW_RESPONSE_BYTES
            ):
                return _not_found_response()
        except Exception:
            return _not_found_response()
        headers = _base_headers()
        headers["Content-Type"] = "image/png"
        headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        headers["Cross-Origin-Resource-Policy"] = "cross-origin"
        headers["Access-Control-Allow-Origin"] = "*"
        return web.Response(status=200, body=data, headers=headers)

    async def meta_json(request: web.Request) -> web.Response:
        limited = public_gate(request)
        if limited is not None:
            return limited
        token = request.match_info.get("token", "")
        if not _valid_token(token):
            return _not_found_response()
        try:
            artifact = await asyncio.to_thread(store.get_public, token)
            body = json.dumps(
                _public_meta(artifact), ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
        except Exception:
            return _not_found_response()
        headers = _base_headers()
        headers["Content-Type"] = "application/json; charset=utf-8"
        headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        headers["Cross-Origin-Resource-Policy"] = "same-origin"
        return web.Response(status=200, body=body, headers=headers)

    async def internal_capture(request: web.Request) -> web.Response:
        if not capture_enabled or coordinator is None:
            return _private_json({"error": "capture unavailable"}, status=404)
        if not _is_loopback(request):
            return _private_json({"error": "capture unavailable"}, status=404)
        authorization = request.headers.get("Authorization", "")
        scheme, separator, presented = authorization.partition(" ")
        if scheme != "Bearer" or not separator or not presented or not hmac.compare_digest(presented, capture_token):
            return _private_json({"error": "unauthorized"}, status=401)
        try:
            raw = await request.read()
        except Exception:
            return _private_json({"error": "invalid request"}, status=400)
        if len(raw) > MAX_CAPTURE_BODY_BYTES:
            return _private_json({"error": "invalid request"}, status=400)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _private_json({"error": "invalid request"}, status=400)
        if not isinstance(payload, dict):
            return _private_json({"error": "invalid request"}, status=400)
        token = payload.get("token")
        width = payload.get("width", DEFAULT_CAPTURE_WIDTH)
        if not isinstance(token, str) or not _valid_token(token):
            return _private_json({"error": "invalid request"}, status=400)
        if isinstance(width, bool) or not isinstance(width, int) or not MIN_CAPTURE_WIDTH <= width <= MAX_CAPTURE_WIDTH:
            return _private_json({"error": "invalid request"}, status=400)
        try:
            await asyncio.to_thread(store.get_public, token)
        except Exception:
            return _not_found_response()
        try:
            image = await coordinator.capture(token, width)
        except CaptureBusy:
            return _private_json({"error": "capture busy"}, status=429)
        except CaptureClosed:
            return _private_json({"error": "capture unavailable"}, status=503)
        except CaptureTooLarge:
            return _private_json({"error": "capture exceeds limit"}, status=413)
        except CaptureTimedOut:
            return _private_json({"error": "capture timed out"}, status=504)
        except CaptureFailed:
            return _private_json({"error": "capture failed"}, status=502)
        except asyncio.CancelledError:
            raise
        except Exception:
            return _private_json({"error": "capture failed"}, status=502)
        if type(image) is not bytes or not image.startswith(b"\x89PNG\r\n\x1a\n"):
            return _private_json({"error": "capture failed"}, status=502)
        if len(image) > MAX_CAPTURE_BYTES:
            return _private_json({"error": "capture exceeds limit"}, status=413)
        headers = _base_headers()
        headers["Content-Type"] = "image/png"
        headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        headers["Content-Disposition"] = 'inline; filename="preview.png"'
        return web.Response(status=200, body=image, headers=headers)

    app.router.add_get("/health", health, allow_head=False)
    app.router.add_get("/p/{token}/", viewer, allow_head=True)
    app.router.add_get("/p/{token}/files/{path:.*}", artifact_file, allow_head=True)
    app.router.add_get("/p/{token}/source.zip", source_zip, allow_head=True)
    app.router.add_get("/p/{token}/preview.png", preview_png, allow_head=True)
    app.router.add_get("/p/{token}/meta.json", meta_json, allow_head=True)
    app.router.add_post("/internal/capture", internal_capture)
    return app


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve immutable web-artifact previews")
    parser.add_argument("--root", required=True, type=Path, help="read-only artifact store root")
    parser.add_argument("--host", default="127.0.0.1", help="listen address")
    parser.add_argument("--port", default=8131, type=int, help="listen port")
    parser.add_argument("--public-origin", required=True, help="public HTTPS preview origin")
    parser.add_argument("--no-capture", action="store_true", help="disable the private Playwright capture endpoint")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    capture_token = os.environ.get("WEB_ARTIFACT_CAPTURE_TOKEN", "")
    if not args.no_capture and not capture_token:
        parser.error("WEB_ARTIFACT_CAPTURE_TOKEN is required unless --no-capture is used")
    try:
        store = ArtifactStore(args.root, read_only=True)
        store.initialize()
        app = create_app(
            store,
            public_origin=args.public_origin,
            capture_token=capture_token,
            capture_enabled=not args.no_capture,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    web.run_app(app, host=args.host, port=args.port, access_log=None)
    return 0


__all__ = [
    "BoundedRateLimiter",
    "build_arg_parser",
    "create_app",
    "main",
]
