"""Policy, pinning, byte quotas, and real Unix transport boundaries."""

from __future__ import annotations

import os
import json
import base64
import socket
import asyncio
from pathlib import Path

import httpx
import pytest
import aiohttp

from utils.plugin_workshop_sandbox import egress, worker_http

SEARCH = "https://geocoding-api.open-meteo.com/v1/search?name=Beijing&count=1"
FORECAST = "https://api.open-meteo.com:443/v1/forecast?latitude=40&longitude=116"
UNIX = pytest.mark.skipif(os.name == "nt" or not hasattr(socket, "AF_UNIX"), reason="Requires Unix sockets")


def request_line(url=SEARCH, **extra):
    return json.dumps({"method": "GET", "url": url, **extra}).encode() + b"\n"


@pytest.mark.parametrize("url", [SEARCH, FORECAST])
def test_exact_public_endpoints_remain_usable(url):
    assert egress.validate_request(request_line(url)) == url


@pytest.mark.parametrize(
    "line",
    [
        request_line("http://api.open-meteo.com/v1/forecast"),
        request_line("https://api.open-meteo.com:444/v1/forecast"),
        request_line("https://api.open-meteo.com/v1/forecast/"),
        request_line("https://api.open-meteo.com/v1/../v1/forecast"),
        request_line("https://api.open-meteo.com/%76%31/forecast"),
        request_line("https://api.open-meteo.com.evil.example/v1/forecast"),
        request_line("https://user@api.open-meteo.com/v1/forecast"),
        request_line("https://api.open-meteo.com/v1/forecast#"),
        request_line("https://api.open-meteo.com\n/v1/forecast"),
        request_line(method="POST"),
        request_line(headers={"Authorization": "secret"}),
        request_line(url=[SEARCH]),
        b'{"method":"POST","method":"GET","url":"' + SEARCH.encode() + b'"}\n',
        b"[]\n",
        b'{"method":"GET","url":NaN}\n',
        b"\xff\n",
        request_line()[:-1],
        b" " * egress.MAX_REQUEST_BYTES + b"\n",
    ],
)
def test_protocol_rejects_endpoint_escapes_and_ambiguous_requests(line):
    with pytest.raises(egress.EgressDenied):
        egress.validate_request(line)


def address(value):
    family = socket.AF_INET6 if ":" in value else socket.AF_INET
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (value, 443))


@pytest.mark.parametrize(
    "unsafe",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "100.64.0.1",
        "192.0.0.8",
        "224.0.0.1",
        "::1",
        "fe80::1",
        "fec0::1",
        "3fff::1",
        "::ffff:8.8.8.8",
        "64:ff9b::808:808",
        "2002:0808:0808::1",
    ],
)
async def test_mixed_dns_is_rejected_before_any_address_can_be_used(unsafe):
    async def lookup(host, port):
        return [address("8.8.8.8"), address(unsafe)]

    resolver = egress.PublicResolver(lookup)
    with pytest.raises(egress.EgressDenied, match="non-public"):
        await resolver.resolve("api.open-meteo.com", 443)


async def test_dns_results_are_pinned_for_the_validation_not_resolved_again():
    calls = 0

    async def changing_lookup(host, port):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return [address("8.8.8.8" if calls == 1 else "127.0.0.1")]

    resolver = egress.PublicResolver(changing_lookup)
    first, second = await asyncio.gather(
        resolver.resolve("api.open-meteo.com", 443), resolver.resolve("api.open-meteo.com", 443)
    )
    first[0]["host"] = "127.0.0.1"
    third = await resolver.resolve("api.open-meteo.com", 443)
    assert second[0]["host"] == third[0]["host"] == "8.8.8.8"
    assert calls == 1


class Reply:
    def __init__(self, body=b'{"temperature":21}', *, status=200, content_type="application/json", encoding=None):
        self.body = body
        self.status = status
        self.headers = {"Content-Type": content_type}
        if encoding:
            self.headers["Content-Encoding"] = encoding
        self.content_length = None
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def iter_chunked(self, size):
        for offset in range(0, len(self.body), size):
            yield self.body[offset : offset + size]


class Upstream:
    def __init__(self, reply):
        self.reply = reply

    def get(self, url, *, allow_redirects):
        assert allow_redirects is False
        return self.reply


async def test_real_upstream_errors_keep_their_status_and_body(tmp_path):
    body = b'{"error":true,"reason":"Invalid latitude"}'
    broker = egress.WorkshopEgress(tmp_path, session=Upstream(Reply(body, status=400)))
    reply = await broker._fetch(FORECAST)
    assert reply["status"] == 400
    assert base64.b64decode(reply["body"]) == body


@pytest.mark.parametrize(
    "reply",
    [
        Reply(status=302),
        Reply(content_type="text/html"),
        Reply(encoding="gzip"),
        Reply(b"not JSON"),
        Reply(b'{"value":NaN}'),
    ],
)
async def test_redirects_and_non_json_upstream_replies_never_become_success(tmp_path, reply):
    broker = egress.WorkshopEgress(tmp_path, session=Upstream(reply))
    with pytest.raises((egress.EgressDenied, json.JSONDecodeError)):
        await broker._fetch(SEARCH)


async def test_per_response_and_lifetime_byte_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(egress, "MAX_RESPONSE_BYTES", 20)
    monkeypatch.setattr(egress, "MAX_TOTAL_BYTES", 30)
    oversized = egress.WorkshopEgress(tmp_path, session=Upstream(Reply(b'"' + b"x" * 20 + b'"')))
    with pytest.raises(egress.EgressDenied, match="quota"):
        await oversized._fetch(SEARCH)
    broker = egress.WorkshopEgress(tmp_path, session=Upstream(Reply(b'"' + b"x" * 14 + b'"')))
    await broker._fetch(SEARCH)
    with pytest.raises(egress.EgressDenied, match="quota"):
        await broker._fetch(SEARCH)


async def exchange(path: Path, line: bytes):
    reader, writer = await asyncio.open_unix_connection(str(path))
    try:
        writer.write(line)
        await writer.drain()
        return json.loads(await reader.readline())
    finally:
        writer.close()
        await writer.wait_closed()


@UNIX
async def test_bad_requests_consume_quota_and_socket_is_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(egress, "MAX_REQUESTS", 2)

    async def fetch(self, url):
        return {"ok": True, "status": 200, "content_type": "application/json", "body": "e30="}

    monkeypatch.setattr(egress.WorkshopEgress, "_fetch", fetch)
    async with egress.WorkshopEgress(tmp_path) as broker:
        path = broker.socket_path
        assert path.stat().st_mode & 0o777 == 0o666
        assert path.parent.stat().st_mode & 0o777 == 0o700
        denied = await exchange(path, request_line(method="POST"))
        assert denied["ok"] is False
        accepted = await exchange(path, request_line())
        assert accepted["ok"] is True
        exhausted = await exchange(path, request_line())
        assert exhausted["ok"] is False
        assert "quota" in exhausted["error"]
    assert not path.exists()
    assert not path.parent.exists()


@UNIX
async def test_teardown_cancels_and_awaits_inflight_upstream(tmp_path, monkeypatch):
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def fetch(self, url):
        started.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()

    monkeypatch.setattr(egress.WorkshopEgress, "_fetch", fetch)
    async with egress.WorkshopEgress(tmp_path) as broker:
        reader, writer = await asyncio.open_unix_connection(str(broker.socket_path))
        writer.write(request_line())
        await writer.drain()
        await asyncio.wait_for(started.wait(), 1)
    assert stopped.is_set()
    assert await reader.read() == b""
    writer.close()
    await writer.wait_closed()


@pytest.mark.parametrize(
    "headers", [{"Authorization": "Bearer secret"}, {"Cookie": "session=secret"}, {"Proxy-Authorization": "secret"}]
)
def test_worker_rejects_credentials_before_opening_a_socket(headers):
    with worker_http.install_http_transport(), httpx.Client() as client:
        with pytest.raises(httpx.UnsupportedProtocol, match="credentials"):
            client.get(SEARCH, headers=headers)


@UNIX
async def test_ordinary_sync_async_clients_use_real_broker_and_close_cleanly(tmp_path, monkeypatch):
    requests = []

    async def fetch(self, url):
        requests.append(url)
        return {
            "ok": True,
            "status": 422,
            "content_type": "application/json; charset=utf-8",
            "body": base64.b64encode(b'{"error":true}').decode(),
        }

    monkeypatch.setattr(egress.WorkshopEgress, "_fetch", fetch)
    async with egress.WorkshopEgress(tmp_path) as broker:
        monkeypatch.setattr(worker_http, "_SOCKET_PATH", str(broker.socket_path))
        with worker_http.install_http_transport():
            async with httpx.AsyncClient() as client:
                reply = await client.get(SEARCH)
                assert reply.status_code == 422
                assert reply.json() == {"error": True}

            def synchronous_client():
                with httpx.Client() as client:
                    response = client.get(SEARCH)
                    return response.status_code, response.json()

            assert await asyncio.to_thread(synchronous_client) == (422, {"error": True})
    assert requests == [SEARCH, SEARCH]


def test_disposal_restores_preexisting_transport_behavior(monkeypatch):
    def original(self, request):
        return httpx.Response(201, json={"original": True})

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", original)
    scope = worker_http.install_http_transport()
    scope.dispose()
    scope.dispose()
    with httpx.Client() as client:
        response = client.get(SEARCH)
        assert response.status_code == 201
        assert response.json() == {"original": True}


async def test_aiohttp_weather_streaming_contract_preserves_status_query_and_json(monkeypatch):
    calls = []
    body = json.dumps({"hourly": {"temperature_2m": [21, 22]}}).encode()

    async def exchange_wire(line):
        calls.append(json.loads(line))
        return (
            json.dumps(
                {"ok": True, "status": 200, "content_type": "application/json", "body": base64.b64encode(body).decode()}
            ).encode()
            + b"\n"
        )

    monkeypatch.setattr(worker_http, "_async_exchange", exchange_wire)
    with worker_http.install_http_transport():
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10, connect=5), trust_env=False) as client:
            async with client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={"latitude": 40, "longitude": 116},
                allow_redirects=False,
            ) as response:
                assert isinstance(response, aiohttp.ClientResponse)
                assert response.status == 200
                payload = bytearray()
                async for chunk in response.content.iter_chunked(16384):
                    payload.extend(chunk)
                assert json.loads(payload) == {"hourly": {"temperature_2m": [21, 22]}}
            async with client.get(SEARCH) as response:
                assert await response.json() == json.loads(body)
                assert await response.read() == body
        assert client.closed
        assert response.closed
    assert calls[0] == {"method": "GET", "url": "https://api.open-meteo.com/v1/forecast?latitude=40&longitude=116"}


@pytest.mark.parametrize(
    "options",
    [
        {"headers": {"Authorization": "secret"}},
        {"cookies": {"session": "secret"}},
        {"auth": aiohttp.BasicAuth("user", "password")},
    ],
)
async def test_aiohttp_session_credentials_are_rejected_before_transport(options):
    with worker_http.install_http_transport():
        async with aiohttp.ClientSession(**options) as client:
            with pytest.raises(aiohttp.ClientError, match="credentials"):
                await client.get(SEARCH)


async def test_aiohttp_deadline_cancels_owned_socket_exchange(monkeypatch):
    cancelled = asyncio.Event()

    async def exchange_wire(line):
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    monkeypatch.setattr(worker_http, "_async_exchange", exchange_wire)
    with worker_http.install_http_transport():
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=0.01)) as client:
            with pytest.raises(asyncio.TimeoutError):
                await client.get(SEARCH)
    assert cancelled.is_set()


async def test_broker_limits_concurrent_upstreams(tmp_path, monkeypatch):
    broker = egress.WorkshopEgress(tmp_path)
    full = asyncio.Event()
    release = asyncio.Event()
    active = peak = 0

    async def fetch(self, url):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == egress.MAX_CONCURRENCY:
            full.set()
        try:
            await release.wait()
            return {"ok": True}
        finally:
            active -= 1

    monkeypatch.setattr(egress.WorkshopEgress, "_fetch", fetch)
    readers = [asyncio.StreamReader() for _ in range(egress.MAX_CONCURRENCY + 1)]
    for reader in readers:
        reader.feed_data(request_line())
        reader.feed_eof()
    tasks = [asyncio.create_task(broker._exchange(reader)) for reader in readers]
    try:
        await asyncio.wait_for(full.wait(), 1)
        release.set()
        await asyncio.gather(*tasks)
    finally:
        release.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert peak == egress.MAX_CONCURRENCY


async def test_aiohttp_reports_broker_failure_as_client_error(monkeypatch):
    async def exchange_wire(line):
        return b'{"ok":false,"error":"Upstream HTTPS request failed"}\n'

    monkeypatch.setattr(worker_http, "_async_exchange", exchange_wire)
    with worker_http.install_http_transport():
        async with aiohttp.ClientSession() as client:
            with pytest.raises(aiohttp.ClientError, match="Upstream HTTPS request failed"):
                await client.get(SEARCH)
