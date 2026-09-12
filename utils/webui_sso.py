"""Import-safe ASGI bridge from verified proxy sessions to native WebUI sessions."""

from __future__ import annotations

import re
from typing import Protocol
import asyncio
from hashlib import sha256
from ipaddress import ip_address
from threading import Lock
from urllib.parse import urlsplit
from collections.abc import Callable

import httpx
from starlette.types import Send, Scope, ASGIApp, Message, Receive
from starlette.requests import HTTPConnection
from starlette.responses import Response, JSONResponse

SSO_COOKIE = "__Host-chtholly_sso"
NATIVE_COOKIE = "webui_sid"
_COOKIE_NAME = re.compile(rf"{re.escape(SSO_COOKIE)}(?:_[0-9]+)?\Z")


class NativeSessionStore(Protocol):
    def get(self, sid: str | None) -> object | None: ...
    def create(self, *, ip: str) -> str: ...
    def destroy(self, sid: str) -> bool: ...


class ProxyUnavailable(Exception):
    """The configured authentication authority could not verify the request."""


class InvalidSsoConfiguration(ValueError):
    """SSO settings do not identify a safe public origin and local authority."""


class SsoSessions:
    """Keep authorization in OAuth2 Proxy and issue ordinary native sessions."""

    def __init__(
        self,
        *,
        public_origin: str,
        auth_url: str,
        store: Callable[[], NativeSessionStore],
        session_ttl: int,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        public = urlsplit(public_origin)
        if (
            public.scheme != "https"
            or not public.hostname
            or public.username is not None
            or public.password is not None
            or public.path not in ("", "/")
            or public.query
            or public.fragment
        ):
            raise InvalidSsoConfiguration("SSO public_origin must be an HTTPS origin")
        authority = urlsplit(auth_url)
        try:
            loopback = ip_address(authority.hostname or "").is_loopback
            valid_port = authority.port is None or authority.port > 0
        except ValueError:
            loopback = valid_port = False
        if (
            authority.scheme != "http"
            or not authority.hostname
            or not loopback
            or not valid_port
            or authority.username is not None
            or authority.password is not None
            or authority.path != "/oauth2/auth"
            or authority.query
            or authority.fragment
        ):
            raise InvalidSsoConfiguration("SSO auth_url must be a literal loopback OAuth2 Proxy auth endpoint")
        self.public_origin = public_origin.rstrip("/")
        self._host = public.hostname.lower()
        self._port = public.port or 443
        self._auth_url = auth_url
        self._store = store
        self._ttl = session_ttl
        self._transport = transport
        self._issued: dict[bytes, tuple[str, NativeSessionStore]] = {}
        self._session_lock = Lock()
        self.closed = False

    def is_public(self, connection: HTTPConnection) -> bool:
        url = connection.url
        return url.scheme in ("https", "wss") and url.hostname == self._host and (url.port or 443) == self._port

    async def verify(self, cookies: dict[str, str], *, logout: bool = False) -> bool:
        selected = [f"{name}={value}" for name, value in cookies.items() if _COOKIE_NAME.fullmatch(name)]
        cookie = "; ".join(selected)
        if not cookies.get(SSO_COOKIE) or len(cookie) > 16384 or "\r" in cookie or "\n" in cookie:
            return False

        async def check_authority() -> bool:
            async with httpx.AsyncClient(
                timeout=20.0, trust_env=False, follow_redirects=False, transport=self._transport
            ) as client:
                async with client.stream(
                    "GET",
                    self._auth_url.rsplit("/", 1)[0] + "/sign_out" if logout else self._auth_url,
                    headers={"Cookie": cookie, "X-Forwarded-Proto": "https"},
                ) as response:
                    if response.status_code == (302 if logout else 202):
                        return True
                    if response.status_code in (401, 403):
                        return False
                    raise ProxyUnavailable

        try:
            return await asyncio.wait_for(check_authority(), timeout=20.0)
        except (httpx.HTTPError, asyncio.TimeoutError):
            raise ProxyUnavailable from None

    @staticmethod
    def _session_key(connection: HTTPConnection) -> bytes:
        # OAuth2 Proxy signs base64(ticket)|createdAt|signature. Redis refresh
        # retains the ticket but changes the last two fields. This is only an
        # identity key AFTER fresh authority verification, never authorization.
        ticket = connection.cookies[SSO_COOKIE].split("|", 1)[0]
        return sha256(ticket.encode("utf-8")).digest()

    def session(self, connection: HTTPConnection) -> tuple[str, bool]:
        key = self._session_key(connection)
        store = self._store()
        # No await separates lookup and creation; the lock also protects native
        # identity issuance if callers run on different ASGI event-loop threads.
        with self._session_lock:
            issued = self._issued.get(key)
            if issued is not None and issued[1] is store and store.get(issued[0]) is not None:
                sid = issued[0]
            else:
                for expired_key, (old_sid, owner) in tuple(self._issued.items()):
                    if owner is not store or owner.get(old_sid) is None:
                        owner.destroy(old_sid)
                        del self._issued[expired_key]
                sid = store.create(ip=connection.client.host if connection.client else "unknown")
                self._issued[key] = (sid, store)
        return sid, sid != connection.cookies.get(NATIVE_COOKIE)

    def cookie(self, sid: str) -> bytes:
        response = Response()
        response.set_cookie(NATIVE_COOKIE, sid, max_age=self._ttl, secure=True, httponly=True, samesite="lax", path="/")
        return next(value for key, value in response.raw_headers if key == b"set-cookie")

    async def logout(self, connection: HTTPConnection) -> Response:
        try:
            revoked = await self.verify(connection.cookies, logout=True)
        except ProxyUnavailable:
            revoked = False
        if not revoked:
            return JSONResponse(
                {"success": False, "code": "sso_unavailable"},
                status_code=503,
                headers={"Cache-Control": "private, no-store"},
            )
        with self._session_lock:
            issued = self._issued.pop(self._session_key(connection), None)
            if issued is not None:
                issued[1].destroy(issued[0])
            sid = connection.cookies.get(NATIVE_COOKIE)
            if sid:
                self._store().destroy(sid)
        response = JSONResponse({"success": True}, headers={"Cache-Control": "private, no-store"})
        names = {NATIVE_COOKIE, SSO_COOKIE}
        names.update(name for name in connection.cookies if _COOKIE_NAME.fullmatch(name))
        for name in names:
            response.delete_cookie(name, path="/", secure=True, httponly=True, samesite="lax")
        return response

    def close(self) -> None:
        with self._session_lock:
            self.closed = True
            for sid, store in self._issued.values():
                store.destroy(sid)
            self._issued.clear()


class WebUISsoMiddleware:
    def __init__(self, app: ASGIApp, sessions: SsoSessions) -> None:
        self.app = app
        self.sessions = sessions

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        connection = HTTPConnection(scope)
        if not self.sessions.is_public(connection):
            await self.app(scope, receive, send)
            return
        status = 401
        try:
            if self.sessions.closed:
                raise ProxyUnavailable
            authenticated = await self.sessions.verify(connection.cookies)
            if self.sessions.closed:
                raise ProxyUnavailable
        except ProxyUnavailable:
            authenticated = False
            status = 503
        if not authenticated:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1013 if status == 503 else 1008})
            else:
                response = JSONResponse(
                    {"success": False, "code": "sso_unavailable" if status == 503 else "authentication_required"},
                    status_code=status,
                    headers={"Cache-Control": "private, no-store"},
                )
                await response(scope, receive, send)
            return
        if scope["type"] == "websocket" or scope.get("method") not in ("GET", "HEAD", "OPTIONS"):
            if connection.headers.get("origin") != self.sessions.public_origin or connection.headers.get(
                "sec-fetch-site", "same-origin"
            ) not in ("same-origin", "none"):
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                else:
                    await JSONResponse({"success": False, "code": "origin_rejected"}, status_code=403)(
                        scope, receive, send
                    )
                return
        if scope["type"] == "http" and scope.get("method") == "POST" and scope["path"] == "/api/auth/logout":
            response = await self.sessions.logout(connection)
            await response(scope, receive, send)
            return
        sid, created = self.sessions.session(connection)
        if not created:
            await self.app(scope, receive, send)
            return
        cookies = dict(connection.cookies)
        cookies[NATIVE_COOKIE] = sid
        headers = [(name, value) for name, value in scope["headers"] if name.lower() != b"cookie"]
        headers.append((b"cookie", "; ".join(f"{name}={value}" for name, value in cookies.items()).encode("latin-1")))
        forwarded = dict(scope, headers=headers)

        async def authenticated_send(message: Message) -> None:
            if message["type"] in ("http.response.start", "websocket.accept"):
                message = dict(message)
                message["headers"] = [*message.get("headers", []), (b"set-cookie", self.sessions.cookie(sid))]
            await send(message)

        await self.app(forwarded, receive, authenticated_send)
