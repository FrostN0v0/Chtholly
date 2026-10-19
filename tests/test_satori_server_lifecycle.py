"""Keep shared HTTP routes alive when a sideloaded protocol adapter stops."""

from typing import Literal, ClassVar
import asyncio
from pathlib import Path

import httpx
import pytest
from launart import Launart, Service
from satori.model import Login
from satori.server import Server, Request
from launart.status import Phase
from starlette.routing import Route
from starlette.responses import Response, FileResponse, JSONResponse
from satori.server.adapter import Adapter


class HostLifetime(Service):
    id = "test.host-lifetime"
    required: ClassVar[set[str]] = set()
    stages: ClassVar[set[Phase]] = {"blocking"}

    async def launch(self, manager: Launart) -> None:
        async with self.stage("blocking"):
            await manager.status.wait_for_sigexit()


class StoppableAdapter(Adapter):
    required: ClassVar[set[str]] = {"satori-python.server", "asgi.service/uvicorn"}
    stages: ClassVar[set[Phase]] = {"preparing", "blocking", "cleanup"}

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    def get_platform(self) -> str:
        return "test"

    def ensure(self, platform: str, self_id: str) -> bool:
        return False

    async def get_logins(self) -> list[Login]:
        return []

    async def handle_internal(self, request: Request, path: str) -> Response:
        raise NotImplementedError

    async def launch(self, manager: Launart) -> None:
        async with self.stage("preparing"):
            pass
        async with self.stage("blocking"):
            await self.release.wait()
        async with self.stage("cleanup"):
            pass


@pytest.mark.parametrize("stop", ["complete", "unload"])
async def test_adapter_stop_preserves_shared_http(stop: Literal["complete", "unload"], tmp_path: Path) -> None:
    document = tmp_path / "index.html"
    document.write_text("<!doctype html><title>Management</title>", encoding="utf-8")

    async def page(request) -> FileResponse:
        return FileResponse(document)

    async def health(request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    server = Server(port=0, path="satori", uvicorn_options={"loop": "asyncio", "access_log": False})
    server.app.routes.extend([Route("/", page), Route("/api/health", health)])
    first, second = StoppableAdapter(), StoppableAdapter()
    server.apply(first)
    server.apply(second)
    manager = Launart()
    manager.add_component(HostLifetime())
    manager.add_component(server.asgi_service)
    launch = asyncio.create_task(manager.launch())
    await asyncio.wait_for(manager.status.wait_for_blocking(), 5)
    manager.add_component(server)
    server_task = None
    try:
        await asyncio.wait_for(server.status.wait_for("blocking"), 5)
        await asyncio.wait_for(first.status.wait_for("blocking"), 5)
        await asyncio.wait_for(second.status.wait_for("blocking"), 5)
        server_task = manager.tasks[server.id]
        first_tracker = manager.task_group.sideload_trackers[first.id]
        transport = httpx.ASGITransport(app=server.asgi_service.middleware)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            response = await client.get("/")
            assert response.status_code == 200
            assert response.text == document.read_text(encoding="utf-8")
            if stop == "unload":
                manager.remove_component(first)
            first.release.set()
            await asyncio.wait_for(first_tracker, 5)
            done, _ = await asyncio.wait([server_task], timeout=0.05)
            assert not done, "One adapter stopped the shared HTTP server"
            assert second.status.blocking
            response = await client.get("/api/health")
            assert response.status_code == 200
            assert response.json() == {"status": "ok"}
            response = await client.get("/")
            assert response.status_code == 200
            assert response.text == document.read_text(encoding="utf-8")
            assert (await client.get("/missing")).status_code == 404
    finally:
        for adapter in (first, second):
            tracker = manager.task_group.sideload_trackers.get(adapter.id)
            if tracker is not None:
                manager.remove_component(adapter)
                adapter.release.set()
                await asyncio.wait_for(tracker, 5)
        tracker = manager.task_group.sideload_trackers.get(server.id)
        if tracker is not None:
            manager.remove_component(server)
            await asyncio.wait_for(tracker, 5)
        manager.status.exiting = True
        await asyncio.wait_for(launch, 10)
    assert server.session.closed
    assert "" not in server.asgi_service.middleware.mounts
    assert server_task is not None
    assert server_task.done()
