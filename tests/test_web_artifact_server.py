"""Behavioral tests for the standalone web-artifact preview server."""

from __future__ import annotations

import re
import json
import base64
import asyncio
from hashlib import sha256

import pytest
from aiohttp.test_utils import TestClient, TestServer

from utils.web_artifacts_core import ArtifactOwner, ArtifactStore
from utils.web_artifacts_server import BoundedRateLimiter, create_app
from utils.web_artifacts_server.capture import MAX_CAPTURE_BYTES

PNG_HEADER = b"\x89PNG\r\n\x1a\n"


def _make_store(tmp_path, clock):
    owner = ArtifactOwner(scope_id=7, user_id="member-9")
    writer = ArtifactStore(tmp_path, clock=clock)
    writer.initialize()
    artifact = writer.publish(
        owner,
        "Demo <title>",
        [
            {"path": "index.html", "content": "<main>Hello</main>"},
            {"path": "assets/app.js", "content": "document.body.dataset.ready = 'yes';"},
        ],
        ttl_hours=1,
        turn_key="server-test",
    )
    writer.close()
    return ArtifactStore(tmp_path, clock=clock, read_only=True), artifact


@pytest.mark.asyncio
async def test_public_viewer_file_headers_and_document_redirect(tmp_path) -> None:
    now = [1_000.0]
    store, artifact = _make_store(tmp_path, lambda: now[0])
    app = create_app(store, public_origin="https://preview.example", capture_token="capture-secret")

    async with TestClient(TestServer(app)) as client:
        viewer = await client.get(f"/p/{artifact.token}/")
        assert viewer.status == 200
        assert "frame-src https://preview.example/p/" in viewer.headers["Content-Security-Policy"]
        assert viewer.headers["Cache-Control"] == "no-store"
        assert viewer.headers["Referrer-Policy"] == "no-referrer"
        assets = re.findall(r"<(style|script)>(.*?)</\1>", await viewer.text(), flags=re.DOTALL)
        assert {kind for kind, _ in assets} == {"script", "style"}
        for kind, content in assets:
            digest = base64.b64encode(sha256(content.encode("utf-8")).digest()).decode("ascii")
            assert f"{kind}-src 'sha256-{digest}'" in viewer.headers["Content-Security-Policy"]

        direct = await client.get(
            f"/p/{artifact.token}/files/index.html",
            headers={"Sec-Fetch-Dest": "document"},
            allow_redirects=False,
        )
        assert direct.status == 302
        assert direct.headers["Location"] == f"/p/{artifact.token}/"

        source = await client.get(f"/p/{artifact.token}/files/index.html")
        assert source.status == 200
        assert await source.text() == "<main>Hello</main>"
        assert source.headers["Access-Control-Allow-Origin"] == "*"
        assert "Access-Control-Allow-Credentials" not in source.headers
        source_csp = source.headers["Content-Security-Policy"]
        assert "sandbox allow-scripts" in source_csp
        assert "frame-ancestors https://preview.example" in source_csp
        assert "connect-src https://preview.example/p/" in source_csp

        meta = await client.get(f"/p/{artifact.token}/meta.json")
        assert meta.status == 200
        payload = json.loads(await meta.text())
        assert payload["title"] == "Demo <title>"
        assert payload["version"] == 1
        assert payload["entry"] == "index.html"
        assert "artifact_ref" not in payload
        assert "token" not in payload
        assert "source_sha256" not in payload


@pytest.mark.asyncio
async def test_unknown_and_expired_public_capabilities_have_uniform_404(tmp_path) -> None:
    now = [2_000.0]
    store, artifact = _make_store(tmp_path, lambda: now[0])
    app = create_app(store, public_origin="https://preview.example", capture_token="capture-secret")
    unknown = "wt_aaaaaaaaaaaaaaaaaaaa"

    async with TestClient(TestServer(app)) as client:
        unknown_response = await client.get(f"/p/{unknown}/")
        assert unknown_response.status == 404
        unknown_body = await unknown_response.read()

        now[0] = artifact.expires_at + 1
        expired_response = await client.get(f"/p/{artifact.token}/source.zip")
        assert expired_response.status == 404
        assert await expired_response.read() == unknown_body

        expired_file = await client.get(f"/p/{artifact.token}/files/index.html")
        assert expired_file.status == 404
        assert await expired_file.read() == unknown_body


@pytest.mark.asyncio
async def test_capture_endpoint_auth_width_queue_and_png_limit(tmp_path) -> None:
    now = [3_000.0]
    store, artifact = _make_store(tmp_path, lambda: now[0])
    started = asyncio.Event()
    release = asyncio.Event()

    async def renderer(_token: str, _width: int) -> bytes:
        started.set()
        await release.wait()
        return PNG_HEADER

    app = create_app(
        store,
        public_origin="https://preview.example",
        capture_token="capture-secret",
        capture_renderer=renderer,
        capture_max_queue=0,
        capture_wall_time=2,
    )

    async with TestClient(TestServer(app)) as client:
        unauthorized = await client.post(
            "/internal/capture",
            json={"token": artifact.token},
        )
        assert unauthorized.status == 401
        assert "capture-secret" not in await unauthorized.text()

        invalid_width = await client.post(
            "/internal/capture",
            headers={"Authorization": "Bearer capture-secret"},
            json={"token": artifact.token, "width": 479},
        )
        assert invalid_width.status == 400

        first = asyncio.create_task(
            client.post(
                "/internal/capture",
                headers={"Authorization": "Bearer capture-secret"},
                json={"token": artifact.token, "width": 900},
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        busy = await client.post(
            "/internal/capture",
            headers={"Authorization": "Bearer capture-secret"},
            json={"token": artifact.token, "width": 900},
        )
        assert busy.status == 429
        release.set()
        first_response = await first
        assert first_response.status == 200
        assert await first_response.read() == PNG_HEADER

    oversized_app = create_app(
        store,
        public_origin="https://preview.example",
        capture_token="capture-secret",
        capture_renderer=lambda _token, _width: asyncio.sleep(0, result=PNG_HEADER + b"x" * MAX_CAPTURE_BYTES),
    )
    async with TestClient(TestServer(oversized_app)) as client:
        oversized = await client.post(
            "/internal/capture",
            headers={"Authorization": "Bearer capture-secret"},
            json={"token": artifact.token},
        )
        assert oversized.status == 413


async def test_rate_limiter_is_bounded_and_refills() -> None:
    now = [0.0]
    limiter = BoundedRateLimiter(max_entries=2, rate_per_second=1, burst=1, clock=lambda: now[0])
    assert limiter.allow("one")
    assert not limiter.allow("one")
    assert limiter.size == 1
    now[0] = 1.0
    assert limiter.allow("one")
    assert limiter.allow("two")
    assert limiter.allow("three")
    assert limiter.size <= 2


async def test_forwarded_clients_have_independent_public_rate_limits(tmp_path) -> None:
    store, artifact = _make_store(tmp_path, lambda: 3_000.0)
    app = create_app(
        store,
        public_origin="https://preview.example",
        capture_token="capture-secret",
        rate_limiter=BoundedRateLimiter(rate_per_second=0.01, burst=1),
    )
    async with TestClient(TestServer(app)) as client:
        attacker = {"X-Artifact-Client-IP": "203.0.113.10"}
        other = {"X-Artifact-Client-IP": "203.0.113.11"}
        first = await client.get("/p/invalid/", headers=attacker)
        assert first.status == 404
        throttled = await client.get(f"/p/{artifact.token}/", headers=attacker)
        assert throttled.status == 429
        unaffected = await client.get(f"/p/{artifact.token}/", headers=other)
        assert unaffected.status == 200


async def test_capture_reads_complete_fragmented_json_body(tmp_path) -> None:
    store, artifact = _make_store(tmp_path, lambda: 3_000.0)
    app = create_app(
        store,
        public_origin="https://preview.example",
        capture_token="capture-secret",
        capture_renderer=lambda _token, _width: asyncio.sleep(0, result=PNG_HEADER),
    )

    async def chunks():
        yield b'{"token":"'
        await asyncio.sleep(0.02)
        yield artifact.token.encode("ascii") + b'"}'

    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/internal/capture", data=chunks(), headers={"Authorization": "Bearer capture-secret"}
        )
        assert response.status == 200
        assert await response.read() == PNG_HEADER


async def test_capture_deadline_includes_waiting_for_previous_cleanup() -> None:
    from utils.web_artifacts_server.capture import CaptureTimedOut, CaptureCoordinator

    started = asyncio.Event()
    cleaning = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def renderer(_token: str, _width: int) -> bytes:
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
        return PNG_HEADER

    coordinator = CaptureCoordinator(renderer, wall_time=0.15)
    first = asyncio.create_task(coordinator.capture("first", 900))
    await started.wait()
    queued = asyncio.create_task(coordinator.capture("queued", 900))
    try:
        await asyncio.wait_for(cleaning.wait(), 1)
        with pytest.raises(CaptureTimedOut):
            await asyncio.wait_for(queued, 0.5)
    finally:
        release.set()
        await asyncio.gather(first, queued, return_exceptions=True)
        await coordinator.close()
    assert calls == 1
