"""Managed runtime service for versioned web artifacts.

The Entari process owns storage and talks to the standalone preview service only
through its authenticated loopback capture endpoint.  No public HTTP server or
untrusted browser is started in this process.
"""

from __future__ import annotations

from typing import TypeVar, Protocol, ParamSpec
import asyncio
from pathlib import Path
import ipaddress
import contextlib
from dataclasses import dataclass
from urllib.parse import quote, urlsplit
from collections.abc import Mapping, Callable, Sequence

import httpx
from launart import Launart, Service
from launart.status import Phase

from utils.web_artifacts_core import Artifact, ArtifactOwner, ArtifactStore

MAX_PREVIEW_BYTES = 3 * 1024 * 1024
MIN_CAPTURE_WIDTH = 480
MAX_CAPTURE_WIDTH = 1200
DEFAULT_CAPTURE_WIDTH = 900
DEFAULT_PURGE_INTERVAL_SECONDS = 300.0
CAPTURE_TIMEOUT_SECONDS = 25.0
CommitObserver = Callable[[Artifact, str], object]
P = ParamSpec("P")
T = TypeVar("T")


async def _wait_for_task(task: asyncio.Task[T], *, propagate_cancellation: bool) -> T:
    """Join a task even when the waiting caller receives repeated cancellation."""

    pending_cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            if pending_cancellation is None:
                pending_cancellation = exc
    result = task.result()
    if pending_cancellation is not None and propagate_cancellation:
        raise pending_cancellation
    return result


class ArtifactServiceError(RuntimeError):
    """Sanitized runtime/service failure."""


class ArtifactCaptureError(ArtifactServiceError):
    """The private capture service could not provide a usable thumbnail."""


class ArtifactCaptureUnavailable(ArtifactCaptureError):
    """Capture is not configured or the service is temporarily unavailable."""


class CaptureClient(Protocol):
    """Minimal injectable seam for the authenticated capture endpoint."""

    async def capture(self, token: str, width: int = DEFAULT_CAPTURE_WIDTH) -> bytes: ...

    async def close(self) -> None: ...


WarningSink = Callable[[str], object]


def normalize_public_origin(value: object) -> str:
    """Validate and canonicalize the public HTTPS origin used in links."""

    if not isinstance(value, str):
        raise ValueError("web artifact public origin must be an HTTPS URL")
    candidate = value.strip()
    if not candidate:
        raise ValueError("web artifact public origin must be an HTTPS URL")
    try:
        parsed = urlsplit(candidate)
    except ValueError as exc:
        raise ValueError("web artifact public origin must be an HTTPS URL") from exc
    if parsed.scheme.casefold() != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("web artifact public origin must be an HTTPS URL")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("web artifact public origin must be a bare HTTPS origin")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("web artifact public origin has an invalid port") from exc
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("web artifact public origin must include a host")
    host = f"[{hostname}]" if ":" in hostname else hostname.lower()
    if port is not None and port != 443:
        host = f"{host}:{port}"
    return f"https://{host}"


def normalize_capture_width(width: object) -> int:
    """Clamp the renderer viewport to the standalone service contract."""

    if type(width) is not int or not MIN_CAPTURE_WIDTH <= width <= MAX_CAPTURE_WIDTH:
        raise ArtifactCaptureError(
            f"capture width must be an integer between {MIN_CAPTURE_WIDTH} and {MAX_CAPTURE_WIDTH}"
        )
    return width


def _is_png(data: bytes) -> bool:
    return data.startswith(b"\x89PNG\r\n\x1a\n")


def _is_loopback_capture_endpoint(endpoint: str) -> bool:
    try:
        parsed = urlsplit(endpoint)
        hostname = parsed.hostname
    except ValueError:
        return False
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path != "/internal/capture"
        or not hostname
    ):
        return False
    try:
        parsed.port
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


class PrivateCaptureHTTPClient:
    """Authenticated client for the private standalone preview capture route."""

    def __init__(
        self,
        endpoint: str,
        capture_token: str,
        *,
        timeout_seconds: float = CAPTURE_TIMEOUT_SECONDS,
        client_factory: Callable[..., httpx.AsyncClient] = httpx.AsyncClient,
    ) -> None:
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise ValueError("capture endpoint is required")
        if not isinstance(capture_token, str) or not capture_token.strip():
            raise ValueError("capture token is required")
        normalized_endpoint = endpoint.strip()
        if not _is_loopback_capture_endpoint(normalized_endpoint):
            raise ValueError("capture endpoint must be a loopback /internal/capture URL")
        self.endpoint = normalized_endpoint
        self._capture_token = capture_token.strip()
        try:
            timeout = float(timeout_seconds)
        except (TypeError, ValueError):
            timeout = CAPTURE_TIMEOUT_SECONDS
        self._timeout_seconds = max(1.0, min(CAPTURE_TIMEOUT_SECONDS, timeout))
        self._client_factory = client_factory
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._requests_idle = asyncio.Event()
        self._requests_idle.set()
        self._active_requests = 0
        self._closed = False

    async def _acquire_client(self) -> httpx.AsyncClient:
        async with self._client_lock:
            if self._closed:
                raise ArtifactCaptureUnavailable("capture client is closed")
            self._active_requests += 1
            self._requests_idle.clear()
            try:
                if self._client is None:
                    self._client = self._client_factory(
                        timeout=self._timeout_seconds,
                        follow_redirects=False,
                        trust_env=False,
                        headers={"Accept": "image/png"},
                    )
                return self._client
            except BaseException:
                self._release_request()
                raise

    def _release_request(self) -> None:
        self._active_requests -= 1
        if self._active_requests <= 0:
            self._active_requests = 0
            self._requests_idle.set()

    async def capture(self, token: str, width: int = DEFAULT_CAPTURE_WIDTH) -> bytes:
        """Capture one artifact through the authenticated private route."""

        if not isinstance(token, str) or not token.strip():
            raise ArtifactCaptureError("preview capture token is unavailable")
        normalized_width = normalize_capture_width(width)
        client = await self._acquire_client()
        data = b""

        try:
            try:
                async with client.stream(
                    "POST",
                    self.endpoint,
                    json={"token": token, "width": normalized_width},
                    headers={"Authorization": f"Bearer {self._capture_token}"},
                ) as response:
                    if response.status_code != 200:
                        raise ArtifactCaptureUnavailable("preview capture service did not return a thumbnail")
                    buffer = bytearray()
                    async for chunk in response.aiter_bytes():
                        if not isinstance(chunk, bytes):
                            raise ArtifactCaptureError("preview capture returned an invalid thumbnail")
                        if len(chunk) > MAX_PREVIEW_BYTES - len(buffer):
                            raise ArtifactCaptureError("preview capture returned an oversized thumbnail")
                        buffer.extend(chunk)
                    data = bytes(buffer)
            except asyncio.CancelledError:
                raise
            except ArtifactCaptureError:
                raise
            except httpx.HTTPError as exc:
                raise ArtifactCaptureUnavailable("preview capture service is unavailable") from exc
            except Exception as exc:
                raise ArtifactCaptureUnavailable("preview capture service is unavailable") from exc
        finally:
            self._release_request()
        if not data or not _is_png(data):
            raise ArtifactCaptureError("preview capture returned an invalid thumbnail")
        return data

    async def close(self) -> None:
        async with self._close_lock:
            async with self._client_lock:
                if self._closed and self._client is None:
                    return
                self._closed = True
            await self._requests_idle.wait()
            async with self._client_lock:
                client, self._client = self._client, None
            if client is not None:
                close_task: asyncio.Task[None] = asyncio.create_task(
                    client.aclose(),
                    name="llm-chat-web-artifact-capture-close",
                )
                try:
                    await asyncio.shield(close_task)
                except asyncio.CancelledError:
                    try:
                        await _wait_for_task(close_task, propagate_cancellation=False)
                    except BaseException:
                        pass
                    raise
                except Exception:
                    # Closing is best effort; the artifact store remains durable.
                    pass


@dataclass(frozen=True, slots=True)
class ArtifactLinks:
    """Public capability links derived only at the user-facing boundary."""

    preview_url: str
    download_url: str
    thumbnail_url: str


class WebArtifactService(Service):
    """Own the local store, capture client, and bounded expiration worker."""

    id = "llm_chat.web_artifacts.service"

    def __init__(
        self,
        root: Path,
        *,
        public_origin: str,
        capture_url: str = "",
        capture_token: str = "",
        ttl_hours: int = 24,
        store: ArtifactStore | None = None,
        capture_client: CaptureClient | None = None,
        warn: WarningSink | None = None,
        purge_interval_seconds: float = DEFAULT_PURGE_INTERVAL_SECONDS,
    ) -> None:
        super().__init__()
        self.root = Path(root)
        self.public_origin = normalize_public_origin(public_origin)
        self.capture_url = capture_url.strip() if isinstance(capture_url, str) else ""
        self.capture_token = capture_token.strip() if isinstance(capture_token, str) else ""
        self.ttl_hours = max(1, min(168, ttl_hours)) if type(ttl_hours) is int else 24
        self.store = store if store is not None else ArtifactStore(self.root)
        if capture_client is not None:
            self.capture_client = capture_client
        elif self.capture_url and self.capture_token:
            self.capture_client = PrivateCaptureHTTPClient(self.capture_url, self.capture_token)
        else:
            self.capture_client = None
        self.warn = warn or (lambda _message: None)
        try:
            interval = float(purge_interval_seconds)
        except (TypeError, ValueError):
            interval = DEFAULT_PURGE_INTERVAL_SECONDS
        self.purge_interval_seconds = max(1.0, interval)
        self._lifecycle_lock = asyncio.Lock()
        self._initialized = False
        self._purge_task: asyncio.Task[None] | None = None
        self._active_operations = 0
        self._operations_idle = asyncio.Event()
        self._operations_idle.set()
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def required(self) -> set[str]:
        return set()

    @property
    def stages(self) -> set[Phase]:
        return {"preparing", "blocking", "cleanup"}

    async def _ensure_started(self) -> None:
        async with self._lifecycle_lock:
            await self._ensure_started_locked()

    async def _ensure_started_locked(self) -> None:
        if self._closed:
            raise ArtifactServiceError("web artifact service is closed")
        if self._initialized:
            return
        await self._thread_call(self.store.initialize)
        try:
            await self._thread_call(self.store.purge_expired)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.warn(f"web artifact expiration purge failed: {type(exc).__name__}")
        self._initialized = True
        self._purge_task = asyncio.create_task(self._purge_loop(), name="llm-chat-web-artifact-purge")

    async def _begin_operation(self, *, initialize: bool = True) -> None:
        async with self._lifecycle_lock:
            if self._closed:
                raise ArtifactServiceError("web artifact service is closed")
            if initialize:
                await self._ensure_started_locked()
            self._active_operations += 1
            self._operations_idle.clear()

    def _end_operation(self) -> None:
        if self._active_operations <= 0:
            return
        self._active_operations -= 1
        if self._active_operations == 0:
            self._operations_idle.set()

    @contextlib.asynccontextmanager
    async def _operation(self, *, initialize: bool = True):
        await self._begin_operation(initialize=initialize)
        try:
            yield
        finally:
            self._end_operation()

    async def _thread_call(self, method: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
        """Run one synchronous store operation and join it if cancelled."""

        task: asyncio.Task[T] = asyncio.create_task(asyncio.to_thread(method, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await _wait_for_task(task, propagate_cancellation=False)
            except BaseException:
                # The caller's cancellation remains authoritative after the
                # worker has been joined and any worker exception consumed.
                pass
            raise

    async def _store_call(self, method: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
        async with self._operation():
            return await self._thread_call(method, *args, **kwargs)

    async def _purge_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.purge_interval_seconds)
            except asyncio.CancelledError:
                raise
            try:
                async with self._operation():
                    await self._thread_call(self.store.purge_expired)
            except asyncio.CancelledError:
                raise
            except ArtifactServiceError:
                return
            except Exception as exc:
                self.warn(f"web artifact expiration purge failed: {type(exc).__name__}")

    def _notify_commit(self, callback: CommitObserver | None, artifact: Artifact, state: str) -> None:
        if callback is None:
            return
        try:
            callback(artifact, state)
        except Exception as exc:
            message = (
                "web artifact commit evidence failed" if state == "published" else "web artifact revoke evidence failed"
            )
            self.warn(f"{message}: {type(exc).__name__}")

    async def publish(
        self,
        owner: ArtifactOwner,
        title: str,
        files: Sequence[Mapping[str, str]],
        *,
        entry: str = "index.html",
        previous_ref: str = "",
        ttl_hours: int | None = None,
        turn_key: str = "",
        delete_paths: Sequence[str] = (),
        on_commit: CommitObserver | None = None,
    ) -> Artifact:
        async with self._operation():
            task: asyncio.Task[Artifact] = asyncio.create_task(
                self._thread_call(
                    self.store.publish,
                    owner,
                    title,
                    files,
                    entry=entry,
                    previous_ref=previous_ref,
                    ttl_hours=self.ttl_hours if ttl_hours is None else ttl_hours,
                    turn_key=turn_key,
                    delete_paths=delete_paths,
                ),
                name="llm-chat-web-artifact-publish",
            )
            try:
                artifact = await asyncio.shield(task)
            except asyncio.CancelledError:
                try:
                    artifact = await _wait_for_task(task, propagate_cancellation=False)
                except BaseException:
                    raise
                self._notify_commit(on_commit, artifact, "published")
                compensation: asyncio.Task[bool] = asyncio.create_task(
                    self._thread_call(self.store.revoke, artifact.artifact_ref, owner, admin=False),
                    name="llm-chat-web-artifact-cancel-revoke",
                )
                try:
                    await _wait_for_task(compensation, propagate_cancellation=False)
                except asyncio.CancelledError:
                    self.warn("web artifact cancellation compensation was cancelled")
                except Exception as exc:
                    self.warn(f"web artifact cancellation compensation failed: {type(exc).__name__}")
                else:
                    self._notify_commit(on_commit, artifact, "revoked")
                raise
            self._notify_commit(on_commit, artifact, "published")
            return artifact

    async def capture_preview(self, artifact: Artifact, *, width: int = DEFAULT_CAPTURE_WIDTH) -> bytes:
        """Capture a PNG thumbnail without weakening storage guarantees."""

        async with self._operation(initialize=False):
            client = self.capture_client
            if client is None:
                raise ArtifactCaptureUnavailable("preview capture is not configured")
            token = artifact.token
            if not token:
                raise ArtifactCaptureUnavailable("preview capture is not configured")
            try:
                data = await asyncio.wait_for(
                    client.capture(token, normalize_capture_width(width)),
                    CAPTURE_TIMEOUT_SECONDS,
                )
            except asyncio.CancelledError:
                raise
            except ArtifactCaptureError:
                raise
            except asyncio.TimeoutError as exc:
                raise ArtifactCaptureUnavailable("preview capture service timed out") from exc
            except Exception as exc:
                raise ArtifactCaptureUnavailable("preview capture service is unavailable") from exc
            if not isinstance(data, bytes) or not data or len(data) > MAX_PREVIEW_BYTES or not _is_png(data):
                raise ArtifactCaptureError("preview capture returned an invalid thumbnail")
            return data

    async def attach_preview(self, artifact_ref: str, owner: ArtifactOwner, data: bytes) -> None:
        await self._store_call(self.store.attach_preview, artifact_ref, owner, data)

    async def get_owned(self, artifact_ref: str, owner: ArtifactOwner, *, admin: bool = False) -> Artifact:
        return await self._store_call(self.store.get_owned, artifact_ref, owner, admin=admin)

    async def list_owned(self, owner: ArtifactOwner, *, admin: bool = False, limit: int = 10) -> list[Artifact]:
        return await self._store_call(self.store.list_owned, owner, admin=admin, limit=limit)

    async def read_owned_file(
        self,
        artifact_ref: str,
        owner: ArtifactOwner,
        path: str,
        *,
        admin: bool = False,
    ) -> tuple[bytes, str]:
        return await self._store_call(self.store.read_owned_file, artifact_ref, owner, path, admin=admin)

    async def zip_owned(self, artifact_ref: str, owner: ArtifactOwner, *, admin: bool = False) -> bytes:
        return await self._store_call(self.store.zip_owned, artifact_ref, owner, admin=admin)

    async def revoke(self, artifact_ref: str, owner: ArtifactOwner, *, admin: bool = False) -> bool:
        return await self._store_call(self.store.revoke, artifact_ref, owner, admin=admin)

    def links_for(self, artifact: Artifact) -> ArtifactLinks:
        """Build expiring public links without exposing private storage paths."""

        token = artifact.token
        if not token:
            raise ArtifactServiceError("artifact public capability is unavailable")
        escaped = quote(token, safe="")
        prefix = f"{self.public_origin}/p/{escaped}"
        return ArtifactLinks(
            preview_url=f"{prefix}/",
            download_url=f"{prefix}/source.zip",
            thumbnail_url=f"{prefix}/preview.png",
        )

    async def _cleanup(self, purge_task: asyncio.Task[None] | None) -> None:
        if purge_task is not None:
            if not purge_task.done():
                purge_task.cancel()
            try:
                await _wait_for_task(purge_task, propagate_cancellation=False)
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                self.warn(f"web artifact expiration task cleanup failed: {type(exc).__name__}")
        await self._operations_idle.wait()
        client, self.capture_client = self.capture_client, None
        if client is not None:
            try:
                await client.close()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.warn(f"web artifact capture client cleanup failed: {type(exc).__name__}")
        try:
            await self._thread_call(self.store.close)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.warn(f"web artifact store cleanup failed: {type(exc).__name__}")

    async def close(self) -> None:
        """Stop managed work and close the private resources exactly once."""

        async with self._lifecycle_lock:
            close_task = self._close_task
            if close_task is None:
                self._closed = True
                purge_task, self._purge_task = self._purge_task, None
                close_task = asyncio.create_task(
                    self._cleanup(purge_task),
                    name="llm-chat-web-artifact-cleanup",
                )
                self._close_task = close_task
        assert close_task is not None
        await _wait_for_task(close_task, propagate_cancellation=True)

    async def launch(self, manager: Launart) -> None:
        async with self.stage("preparing"):
            await self._ensure_started()
        async with self.stage("blocking"):
            await manager.status.wait_for_sigexit()
        async with self.stage("cleanup"):
            await self.close()


__all__ = [
    "ArtifactCaptureError",
    "ArtifactCaptureUnavailable",
    "ArtifactLinks",
    "ArtifactServiceError",
    "CAPTURE_TIMEOUT_SECONDS",
    "CaptureClient",
    "DEFAULT_CAPTURE_WIDTH",
    "MAX_CAPTURE_WIDTH",
    "MAX_PREVIEW_BYTES",
    "MIN_CAPTURE_WIDTH",
    "PrivateCaptureHTTPClient",
    "WebArtifactService",
    "normalize_capture_width",
    "normalize_public_origin",
]
