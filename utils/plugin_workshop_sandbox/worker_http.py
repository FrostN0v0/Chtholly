"""Scoped real HTTP transports through the workshop's sole mounted Unix socket."""

from __future__ import annotations

from http import HTTPStatus
import json
import time
import base64
import socket
from typing import cast
import asyncio
import binascii
from collections.abc import Callable, Awaitable
from typing_extensions import Self

import httpx
import aiohttp
from multidict import CIMultiDict, CIMultiDictProxy
from aiohttp.helpers import TimerNoop, sentinel
from aiohttp.http_writer import StreamWriter
from aiohttp.base_protocol import BaseProtocol

_SOCKET_PATH = "/run/workshop-http.sock"
_MAX_REQUEST_BYTES = 8192
_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_REPLY_BYTES = 2 * _MAX_RESPONSE_BYTES
_TIMEOUT = 17.0
_active = False


def _request_line(request: httpx.Request) -> bytes:
    if request.method != "GET":
        raise httpx.UnsupportedProtocol("Workshop egress permits only GET", request=request)
    if any(name in request.headers for name in ("authorization", "cookie", "proxy-authorization")):
        raise httpx.UnsupportedProtocol("Workshop egress never sends credentials or cookies", request=request)
    if request.headers.get("content-length", "0") != "0" or "transfer-encoding" in request.headers:
        raise httpx.UnsupportedProtocol("Workshop egress does not accept request bodies", request=request)
    if request.url.username or request.url.password or request.url.fragment:
        raise httpx.UnsupportedProtocol("Workshop egress rejects URL credentials and fragments", request=request)
    line = json.dumps({"method": request.method, "url": str(request.url)}, separators=(",", ":")).encode() + b"\n"
    if len(line) > _MAX_REQUEST_BYTES:
        raise httpx.UnsupportedProtocol("Workshop egress request is too large", request=request)
    return line


def _reply(line: bytes, request: httpx.Request) -> httpx.Response:
    try:
        if not line.endswith(b"\n") or len(line) > _MAX_REPLY_BYTES:
            raise ValueError("Invalid reply framing")
        reply = json.loads(line)
        if not isinstance(reply, dict):
            raise ValueError("Invalid reply object")
        if reply.get("ok") is False:
            error = reply.get("error")
            if not isinstance(error, str) or len(error) > 256:
                raise ValueError("Invalid error reply")
            raise httpx.TransportError("Workshop egress: " + error, request=request)
        if reply.get("ok") is not True or set(reply) != {"ok", "status", "content_type", "body"}:
            raise ValueError("Invalid reply fields")
        status, content_type = reply["status"], reply["content_type"]
        if type(status) is not int or not 100 <= status <= 599:
            raise ValueError("Invalid status")
        if not isinstance(content_type, str) or len(content_type) > 128:
            raise ValueError("Invalid content type")
        if any(ord(char) < 32 or ord(char) >= 127 for char in content_type):
            raise ValueError("Invalid header bytes")
        body = base64.b64decode(reply["body"], validate=True)
        if len(body) > _MAX_RESPONSE_BYTES:
            raise ValueError("Reply body is too large")
        return httpx.Response(status, headers={"Content-Type": content_type}, content=body, request=request)
    except (ValueError, TypeError, UnicodeError, RecursionError, binascii.Error) as exc:
        raise httpx.RemoteProtocolError("Invalid workshop egress reply", request=request) from exc


def _sync_request(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
    line = _request_line(request)
    deadline = time.monotonic() + _TIMEOUT
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(_TIMEOUT)
            connection.connect(_SOCKET_PATH)
            connection.sendall(line)
            reply = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Egress deadline expired")
                connection.settimeout(remaining)
                chunk = connection.recv(min(65536, _MAX_REPLY_BYTES + 1 - len(reply)))
                if not chunk:
                    raise httpx.RemoteProtocolError("Workshop egress closed before its reply", request=request)
                reply.extend(chunk)
                if len(reply) > _MAX_REPLY_BYTES:
                    raise httpx.RemoteProtocolError("Workshop egress reply exceeds its limit", request=request)
                if b"\n" in chunk:
                    return _reply(bytes(reply), request)
    except TimeoutError as exc:
        raise httpx.ReadTimeout("Workshop egress timed out", request=request) from exc
    except OSError as exc:
        raise httpx.ConnectError("Workshop egress socket is unavailable", request=request) from exc


async def _async_exchange(line: bytes) -> bytes:
    reader, writer = await asyncio.open_unix_connection(_SOCKET_PATH, limit=_MAX_REPLY_BYTES)
    try:
        writer.write(line)
        await writer.drain()
        return await reader.readline()
    finally:
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout=1)
        except (OSError, asyncio.TimeoutError):
            pass


async def _async_request(self: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
    line = _request_line(request)
    try:
        reply = await asyncio.wait_for(_async_exchange(line), timeout=_TIMEOUT)
        return _reply(reply, request)
    except asyncio.TimeoutError as exc:
        raise httpx.ReadTimeout("Workshop egress timed out", request=request) from exc
    except OSError as exc:
        raise httpx.ConnectError("Workshop egress socket is unavailable", request=request) from exc
    except ValueError as exc:
        raise httpx.RemoteProtocolError("Workshop egress reply exceeds its limit", request=request) from exc


async def _aiohttp_request(self: aiohttp.ClientSession, method, str_or_url, **options):
    if self.closed:
        raise RuntimeError("Session is closed")
    url = self._build_url(str_or_url)
    if options.get("params"):
        url = url.extend_query(options["params"])
    headers = self._prepare_headers(options.get("headers"))
    if (
        self.auth is not None
        or options.get("auth") is not None
        or options.get("proxy_auth") is not None
        or getattr(self, "_default_proxy_auth", None) is not None
        or options.get("cookies")
        or self.cookie_jar.filter_cookies(url)
        or url.user is not None
        or url.password is not None
        or "#" in str(str_or_url)
        or any(name in headers for name in ("Authorization", "Cookie", "Proxy-Authorization"))
    ):
        raise aiohttp.ClientError("Workshop egress rejects credentials, cookies, and fragments")
    if options.get("data") is not None or options.get("json") is not None:
        raise aiohttp.ClientError("Workshop egress does not accept request bodies")
    # Neither environment proxies nor per-request TLS/proxy options reach the broker.
    request = httpx.Request(method, str(url), headers=headers)
    timeout = options.get("timeout", sentinel)
    if timeout is sentinel:
        timeout = self.timeout
    if not isinstance(timeout, aiohttp.ClientTimeout):
        timeout = aiohttp.ClientTimeout(total=timeout)
    deadline = min(_TIMEOUT, timeout.total) if timeout.total is not None and timeout.total > 0 else _TIMEOUT
    try:
        line = _request_line(request)
        wire = await asyncio.wait_for(_async_exchange(line), timeout=deadline)
        received = _reply(wire, request)
    except httpx.HTTPError as exc:
        raise aiohttp.ClientError(str(exc)) from exc
    except asyncio.TimeoutError:
        raise
    except OSError as exc:
        raise aiohttp.ClientConnectionError("Workshop egress socket is unavailable") from exc
    except ValueError as exc:
        raise aiohttp.ClientPayloadError("Invalid workshop egress reply") from exc
    # Materialize the genuine aiohttp response/stream API from bytes actually fetched
    # by the host broker. No fabricated response, replay, or external connector is used.
    loop = asyncio.get_running_loop()
    protocol = BaseProtocol(loop)
    response = aiohttp.ClientResponse(
        method.upper(),
        url,
        writer=None,
        continue100=None,
        timer=TimerNoop(),
        request_info=aiohttp.RequestInfo(url, method.upper(), CIMultiDictProxy(headers), url),
        traces=[],
        loop=loop,
        session=self,
        stream_writer=StreamWriter(protocol, loop),
    )
    response.status = received.status_code
    try:
        response.reason = HTTPStatus(response.status).phrase
    except ValueError:
        response.reason = "Upstream response"
    response.version = aiohttp.HttpVersion11
    response._headers = CIMultiDictProxy(CIMultiDict({"Content-Type": received.headers["Content-Type"]}))
    response._raw_headers = ((b"Content-Type", received.headers["Content-Type"].encode("ascii")),)
    response.content = aiohttp.StreamReader(protocol, limit=_MAX_RESPONSE_BYTES, loop=loop)
    response.content.feed_data(received.content)
    response.content.feed_eof()
    raise_status = options.get("raise_for_status", self._raise_for_status)
    if raise_status is None:
        raise_status = self._raise_for_status
    try:
        if callable(raise_status):
            await cast(Callable[[aiohttp.ClientResponse], Awaitable[None]], raise_status)(response)
        elif raise_status:
            response.raise_for_status()
    except BaseException:
        response.close()
        raise
    return response


class HttpTransportScope:
    """Restore transport request methods without altering constructors or close methods."""

    def __init__(self):
        global _active
        if _active:
            raise RuntimeError("Workshop HTTP transports are already installed")
        self._sync = httpx.HTTPTransport.handle_request
        self._async = httpx.AsyncHTTPTransport.handle_async_request
        self._aiohttp = aiohttp.ClientSession._request
        self._disposed = False
        httpx.HTTPTransport.handle_request = _sync_request
        httpx.AsyncHTTPTransport.handle_async_request = _async_request
        aiohttp.ClientSession._request = _aiohttp_request
        _active = True

    def dispose(self) -> None:
        global _active
        if self._disposed:
            return
        httpx.HTTPTransport.handle_request = self._sync
        httpx.AsyncHTTPTransport.handle_async_request = self._async
        aiohttp.ClientSession._request = self._aiohttp
        self._disposed = True
        _active = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.dispose()


def install_http_transport() -> HttpTransportScope:
    """Route ordinary httpx/aiohttp clients through the fixed mounted broker socket."""
    return HttpTransportScope()
