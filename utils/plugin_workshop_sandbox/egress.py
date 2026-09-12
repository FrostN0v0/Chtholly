"""Per-validation, deny-by-default HTTPS broker for an isolated workshop worker."""

from __future__ import annotations

import os
import json
import base64
import socket
import asyncio
from pathlib import Path
import tempfile
import ipaddress
from urllib.parse import urlsplit
from collections.abc import Callable, Awaitable
from typing_extensions import Self

import aiohttp
from aiohttp.abc import ResolveResult, AbstractResolver

MAX_REQUEST_BYTES = 8192
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 8 * MAX_RESPONSE_BYTES
MAX_REQUESTS = 32
MAX_CONCURRENCY = 4
REQUEST_TIMEOUT = 15
_ALLOWED = {
    "geocoding-api.open-meteo.com": "/v1/search",
    "api.open-meteo.com": "/v1/forecast",
}
# Keep exclusions stable on Python 3.10, whose IANA special-range table is older.
_SPECIAL_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in (
        "192.0.0.0/24",
        "192.88.99.0/24",
        "64:ff9b::/96",
        "64:ff9b:1::/48",
        "2001::/23",
        "2002::/16",
        "3fff::/20",
        "fec0::/10",
    )
)


class EgressDenied(ValueError):
    """The request, DNS result, or upstream reply exceeds broker policy."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise EgressDenied("Duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(value):
    raise EgressDenied("Non-finite JSON value")


def validate_request(line: bytes) -> str:
    """Validate one bounded protocol request and return its unchanged HTTPS URL."""
    if len(line) > MAX_REQUEST_BYTES or not line.endswith(b"\n"):
        raise EgressDenied("Expected one bounded JSON line")
    try:
        request = json.loads(line, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise EgressDenied("Invalid request JSON") from exc
    if not isinstance(request, dict) or set(request) != {"method", "url"}:
        raise EgressDenied("Only method and url fields are accepted")
    if request["method"] != "GET":
        raise EgressDenied("Only GET is permitted")
    url = request["url"]
    if not isinstance(url, str) or not url or len(url) > 4096:
        raise EgressDenied("Invalid URL")
    if any(ord(char) <= 32 or ord(char) >= 127 for char in url) or "#" in url or "\\" in url:
        raise EgressDenied("URL must be ASCII without whitespace, fragments, or backslashes")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        if (
            parsed.scheme != "https"
            or host not in _ALLOWED
            or parsed.netloc not in (host, host + ":443")
            or parsed.port not in (None, 443)
            or parsed.path != _ALLOWED[host]
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise EgressDenied("Endpoint is not permitted")
    except ValueError as exc:
        raise EgressDenied("Endpoint is not permitted") from exc
    return url


def _public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if (
        "%" in value
        or not address.is_global
        or any(
            (
                address.is_multicast,
                address.is_reserved,
                address.is_loopback,
                address.is_link_local,
                address.is_unspecified,
            )
        )
    ):
        return False
    if any(address in network for network in _SPECIAL_NETWORKS):
        return False
    if isinstance(address, ipaddress.IPv6Address):
        # Reject transition/translation ranges, even where Python labels them global.
        if address.is_site_local or address.ipv4_mapped or address.sixtofour or address.teredo:
            return False
    return True


Lookup = Callable[[str, int], Awaitable[list]]


async def _lookup(host: str, port: int) -> list:
    return await asyncio.get_running_loop().getaddrinfo(host, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)


class PublicResolver(AbstractResolver):
    """Validate every A/AAAA answer once, then pin those addresses for this job."""

    def __init__(self, lookup: Lookup | None = None):
        self._lookup = lookup or _lookup
        self._pinned: dict[str, tuple[ResolveResult, ...]] = {}
        self._lock = asyncio.Lock()

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET) -> list[ResolveResult]:
        if host not in _ALLOWED or port != 443:
            raise EgressDenied("DNS endpoint is not permitted")
        async with self._lock:
            if host not in self._pinned:
                answers = await self._lookup(host, port)
                if not answers or any(
                    answer[0] not in (socket.AF_INET, socket.AF_INET6) or not _public_address(answer[4][0])
                    for answer in answers
                ):
                    raise EgressDenied("DNS returned a non-public address")
                self._pinned[host] = tuple(
                    {
                        "hostname": host,
                        "host": answer[4][0],
                        "port": 443,
                        "family": answer[0],
                        "proto": socket.IPPROTO_TCP,
                        "flags": socket.AI_NUMERICHOST,
                    }
                    for answer in answers
                )
            # Never return the cached mutable dictionaries to a connector.
            return [answer.copy() for answer in self._pinned[host]]

    async def close(self) -> None:
        self._pinned.clear()


class WorkshopEgress:
    """Own the socket, DNS pins, quotas, and all connection tasks of one validation."""

    def __init__(self, root: Path, *, session: aiohttp.ClientSession | None = None):
        self.root = Path(root)
        self.socket_path: Path | None = None
        self._directory: Path | None = None
        self._server: asyncio.AbstractServer | None = None
        self._session = session
        self._owns_session = session is None
        self._resolver = PublicResolver()
        self._tasks: set[asyncio.Task] = set()
        self._slots = asyncio.Semaphore(MAX_CONCURRENCY)
        self._accepted = 0
        self._bytes = 0
        self._closing = False
        self._entered = False

    async def __aenter__(self) -> Self:
        if self._entered:
            raise RuntimeError("An egress broker cannot be reused")
        self._entered = True
        if os.name == "nt" or not hasattr(socket, "AF_UNIX"):
            raise RuntimeError("Workshop egress requires Unix sockets; no network fallback is available")
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._directory = Path(tempfile.mkdtemp(prefix="job-", dir=self.root))
            self._directory.chmod(0o700)
            self.socket_path = self._directory / "http.sock"
            if self._session is None:
                connector = aiohttp.TCPConnector(
                    resolver=self._resolver,
                    use_dns_cache=False,
                    limit=MAX_CONCURRENCY,
                    force_close=True,
                )
                self._session = aiohttp.ClientSession(
                    connector=connector,
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                    trust_env=False,
                    cookie_jar=aiohttp.DummyCookieJar(),
                    auto_decompress=False,
                    headers={"Accept": "application/json", "Accept-Encoding": "identity"},
                )
            self._server = await asyncio.start_unix_server(
                self._accept, path=str(self.socket_path), limit=MAX_REQUEST_BYTES
            )
            # The private parent prevents host traversal. Docker mounts ONLY this socket.
            self.socket_path.chmod(0o666)
            return self
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        self._closing = True
        if self._server is not None:
            self._server.close()
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._server is not None:
            await self._server.wait_closed()
        if self._session is not None and self._owns_session:
            await self._session.close()
        await self._resolver.close()
        if self.socket_path is not None:
            self.socket_path.unlink(missing_ok=True)
        if self._directory is not None:
            self._directory.rmdir()

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._closing or self._accepted >= MAX_REQUESTS:
            writer.write(b'{"ok":false,"error":"Request quota exhausted"}\n')
            writer.close()
            return
        # Invalid and stalled connections also consume the lifetime request quota.
        self._accepted += 1
        task = asyncio.create_task(self._serve(reader, writer))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                reply = await asyncio.wait_for(self._exchange(reader), timeout=REQUEST_TIMEOUT)
            except EgressDenied as exc:
                reply = {"ok": False, "error": str(exc)}
            except (asyncio.TimeoutError, TimeoutError):
                reply = {"ok": False, "error": "Egress request timed out"}
            except (ValueError, UnicodeError, RecursionError):
                reply = {"ok": False, "error": "Invalid request or upstream JSON"}
            except Exception:
                # Do not disclose host paths, resolver details, or proxy/environment values.
                reply = {"ok": False, "error": "Upstream HTTPS request failed"}
            writer.write(json.dumps(reply, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n")
            await asyncio.wait_for(writer.drain(), timeout=1)
        except (ConnectionError, OSError, asyncio.TimeoutError):
            pass
        finally:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=1)
            except (ConnectionError, OSError, asyncio.TimeoutError):
                pass

    async def _exchange(self, reader: asyncio.StreamReader) -> dict:
        line = await reader.readline()
        url = validate_request(line)
        async with self._slots:
            return await self._fetch(url)

    async def _fetch(self, url: str) -> dict:
        if self._bytes >= MAX_TOTAL_BYTES:
            raise EgressDenied("Response byte quota exhausted")
        if self._session is None:
            raise RuntimeError("Egress broker is not running")
        async with self._session.get(url, allow_redirects=False) as response:
            if 300 <= response.status < 400:
                raise EgressDenied("Upstream redirects are not permitted")
            content_type = response.headers.get("Content-Type", "")
            media_type = content_type.split(";", 1)[0].strip().lower()
            if (
                len(content_type) > 128
                or any(ord(char) < 32 or ord(char) >= 127 for char in content_type)
                or not (
                    media_type == "application/json"
                    or (media_type.startswith("application/") and media_type.endswith("+json"))
                )
                or response.headers.get("Content-Encoding", "identity").lower() != "identity"
            ):
                raise EgressDenied("Upstream response must be uncompressed JSON")
            if response.content_length is not None and response.content_length > MAX_RESPONSE_BYTES:
                raise EgressDenied("Upstream response exceeds the size limit")
            body = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                self._bytes += len(chunk)
                if self._bytes > MAX_TOTAL_BYTES or len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                    raise EgressDenied("Response byte quota exceeded")
                body.extend(chunk)
            json.loads(body.decode("utf-8"), parse_constant=_reject_constant)
            return {
                "ok": True,
                "status": response.status,
                "content_type": content_type,
                "body": base64.b64encode(body).decode("ascii"),
            }
