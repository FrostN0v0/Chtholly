"""Authorization and lifecycle contracts for the deployment SSO session bridge."""

from __future__ import annotations

from asyncio import gather
from secrets import token_urlsafe
from http.cookies import SimpleCookie

import httpx
import pytest
from fastapi import FastAPI, Request, WebSocket
from starlette.responses import JSONResponse
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from utils.webui_sso import SSO_COOKIE, SsoSessions, WebUISsoMiddleware, InvalidSsoConfiguration

PUBLIC = "https://manage.example"


class Sessions:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}

    def create(self, *, ip: str) -> str:
        sid = token_urlsafe(24)
        self.values[sid] = object()
        return sid

    def get(self, sid: str | None) -> object | None:
        return self.values.get(sid or "")

    def destroy(self, sid: str) -> bool:
        return self.values.pop(sid, None) is not None


def application(*, auth_status: int | None = None):
    store = Sessions()
    authority = {"available": True, "revoked": False}

    def verify(request: httpx.Request) -> httpx.Response:
        if not authority["available"]:
            raise httpx.ConnectError("unavailable", request=request)
        if auth_status is not None:
            return httpx.Response(auth_status)
        cookies = SimpleCookie(request.headers.get("cookie", ""))
        sso = cookies.get(SSO_COOKIE)
        valid = sso is not None and sso.value.split("|", 1)[0] == "verified" and not authority["revoked"]
        if request.url.path == "/oauth2/sign_out":
            authority["revoked"] = True
            return httpx.Response(302, headers={"Location": "/"})
        return httpx.Response(202 if valid else 401)

    sessions = SsoSessions(
        public_origin=PUBLIC,
        auth_url="http://127.0.0.1:4180/oauth2/auth",
        store=lambda: store,
        session_ttl=3600,
        transport=httpx.MockTransport(verify),
    )
    app = FastAPI()
    app.add_middleware(WebUISsoMiddleware, sessions=sessions)

    @app.api_route("/private", methods=["GET", "POST"])
    async def private(request: Request):
        if store.get(request.cookies.get("webui_sid")) is None:
            return JSONResponse({"error": "native_auth_required"}, status_code=401)
        return {"private": "native administrator data"}

    @app.websocket("/ws/logs")
    async def websocket(socket: WebSocket):
        if store.get(socket.cookies.get("webui_sid")) is None:
            await socket.close(code=1008)
            return
        await socket.accept()
        await socket.send_text("native administrator stream")
        await socket.close()

    return app, store, sessions, authority


@pytest.mark.asyncio
async def test_verified_sso_bootstraps_native_requests_and_recovers_expired_sessions():
    app, store, sessions, _ = application()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=PUBLIC) as client:
        client.cookies.set(SSO_COOKIE, "verified")
        first = await client.get("/private")
        assert first.json() == {"private": "native administrator data"}
        cookie = first.headers["set-cookie"]
        assert all(attribute in cookie for attribute in ("webui_sid=", "Secure", "HttpOnly", "SameSite=lax", "Path=/"))
        store.values.clear()
        recovered = await client.get("/private")
        assert recovered.json() == first.json()
        assert "webui_sid" in recovered.cookies
        sessions.close()
        assert (await client.get("/private")).status_code == 503


@pytest.mark.asyncio
async def test_concurrent_bootstrap_and_process_restart_converge_on_one_native_cookie():
    app, store, _, _ = application()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=PUBLIC) as client:
        headers = {"Cookie": f"{SSO_COOKIE}=verified"}
        initial = await gather(*(client.get("/private", headers=headers) for _ in range(8)))
        assert all(response.status_code == 200 for response in initial)
        native = {response.cookies["webui_sid"] for response in initial}
        assert len(native) == 1
        original = native.pop()

        store.values.clear()
        headers["Cookie"] += f"; webui_sid={original}"
        recovered = await gather(*(client.get("/private", headers=headers) for _ in range(8)))
        assert all(response.status_code == 200 for response in recovered)
        replacement = {response.cookies["webui_sid"] for response in recovered}
        assert len(replacement) == 1
        assert replacement != {original}


@pytest.mark.asyncio
async def test_refreshed_redis_cookie_keeps_native_identity_but_never_cached_authorization():
    app, _, _, authority = application()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=PUBLIC) as client:
        original = await client.get("/private", headers={"Cookie": f"{SSO_COOKIE}=verified|100|first-signature"})
        refreshed = await client.get("/private", headers={"Cookie": f"{SSO_COOKIE}=verified|200|second-signature"})
        assert refreshed.status_code == 200
        assert refreshed.cookies["webui_sid"] == original.cookies["webui_sid"]
        authority["revoked"] = True
        rejected = await client.get("/private", headers={"Cookie": f"{SSO_COOKIE}=verified|200|second-signature"})
        assert rejected.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 302, 429, 503])
async def test_unexpected_authority_responses_are_unavailable_not_login_expiry(status):
    app, _, _, _ = application(auth_status=status)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=PUBLIC) as client:
        response = await client.get("/private", headers={"Cookie": f"{SSO_COOKIE}=verified"})
        assert response.status_code == 503
        assert response.json()["code"] == "sso_unavailable"
        assert "webui_sid" not in response.cookies


@pytest.mark.asyncio
async def test_missing_forged_and_revoked_sso_cannot_use_native_or_forwarded_identity():
    app, store, _, authority = application()
    native = store.create(ip="local operator")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=PUBLIC) as client:
        client.cookies.set("webui_sid", native)
        assert (await client.get("/private", headers={"X-Forwarded-User": "admin"})).status_code == 401
        client.cookies.set(SSO_COOKIE, "forged")
        assert (await client.get("/private")).status_code == 401
        client.cookies.set(SSO_COOKIE, "verified")
        assert (await client.get("/private")).status_code == 200
        authority["available"] = False
        assert (await client.get("/private")).status_code == 503
        local = await client.get("http://127.0.0.1/private")
        assert local.status_code == 200


@pytest.mark.asyncio
async def test_public_logout_requires_same_origin_and_revokes_both_login_layers():
    app, store, _, _ = application()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=PUBLIC) as client:
        client.cookies.set(SSO_COOKIE, "verified", domain="manage.example", path="/")
        assert (await client.get("/private")).status_code == 200
        native = client.cookies.get("webui_sid")
        rejected = await client.post("/api/auth/logout", headers={"Origin": "https://elsewhere.example"})
        assert rejected.status_code == 403
        assert (await client.get("/private")).status_code == 200
        logged_out = await client.post("/api/auth/logout", headers={"Origin": PUBLIC})
        assert logged_out.json() == {"success": True}
        assert client.cookies.get(SSO_COOKIE) is None
        assert client.cookies.get("webui_sid") is None
        assert store.get(native) is None
        assert (await client.get("/private")).status_code == 401
        client.cookies.set(SSO_COOKIE, "verified", domain="manage.example", path="/")
        assert (await client.get("/private")).status_code == 401


@pytest.mark.asyncio
async def test_unconfirmed_gateway_logout_never_claims_durable_revocation():
    app, store, _, authority = application()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=PUBLIC) as client:
        client.cookies.set(SSO_COOKIE, "verified")
        assert (await client.get("/private")).status_code == 200
        native = client.cookies.get("webui_sid")
        authority["available"] = False
        response = await client.post("/api/auth/logout", headers={"Origin": PUBLIC})
        assert response.status_code == 503
        assert response.json()["success"] is False
        assert store.get(native) is not None


def test_websocket_uses_real_native_session_and_rejects_cross_origin():
    app, _, _, _ = application()
    with TestClient(app, base_url=PUBLIC) as client:
        headers = {"Cookie": f"{SSO_COOKIE}=verified", "Origin": PUBLIC}
        with client.websocket_connect("wss://manage.example/ws/logs", headers=headers) as websocket:
            assert websocket.receive_text() == "native administrator stream"
        with pytest.raises(WebSocketDisconnect) as rejected:
            with client.websocket_connect(
                "wss://manage.example/ws/logs", headers={**headers, "Origin": "https://elsewhere.example"}
            ):
                pytest.fail("Cross-origin socket was accepted")
        assert rejected.value.code == 1008


def test_websocket_authority_outage_is_retryable_not_an_auth_rejection():
    app, _, _, authority = application()
    authority["available"] = False
    with TestClient(app, base_url=PUBLIC) as client:
        with pytest.raises(WebSocketDisconnect) as rejected:
            with client.websocket_connect(
                "wss://manage.example/ws/logs", headers={"Cookie": f"{SSO_COOKIE}=verified", "Origin": PUBLIC}
            ):
                pytest.fail("Unavailable authority accepted the socket")
        assert rejected.value.code == 1013


@pytest.mark.parametrize("url", ["http://example.com/oauth2/auth", "http://127.0.0.1/"])
def test_authentication_authority_cannot_escape_the_fixed_loopback_endpoint(url):
    with pytest.raises(InvalidSsoConfiguration):
        SsoSessions(public_origin=PUBLIC, auth_url=url, store=Sessions, session_ttl=3600)
