"""Workshop orchestration with durable approval and serialized native changes."""

from __future__ import annotations

import asyncio
from pathlib import Path
from collections.abc import Mapping, Callable
from importlib.metadata import version as distribution_version

from launart import Launart, Service
from launart.status import Phase

from utils.plugin_workshop_core.store import WorkshopStore
from utils.plugin_workshop_core.models import (
    Actor,
    Sandbox,
    CheckResult,
    ProjectState,
    SandboxLimits,
    VersionRecord,
    WorkshopError,
    SandboxRequest,
    ValidationReport,
)
from utils.plugin_workshop_core.policy import parse_manifest

from .config import WorkshopConfig  # entari: package
from .runtime import NativePluginDriver  # entari: package

NATIVE_PERMISSIONS_WARNING = (
    "Approval permits this exact version to execute with full bot process privileges, including data, "
    "credentials, network, and messages. Manifest permissions are declarations, not enforced restrictions. "
    "Container acceptance results are untrusted in-process functional evidence, not a security certification. "
    "Rollback restores plugin code, not external data or messages."
)
_SYSTEM = Actor("workshop:recovery", is_admin=True)


async def _settle(task: asyncio.Task):
    """Finish an owned operation even if its caller is cancelled repeatedly."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


class PluginWorkshopService(Service):
    id = "plugin.workshop"

    def __init__(
        self,
        config: WorkshopConfig,
        *,
        root: Path,
        store: WorkshopStore | None = None,
        sandbox: Sandbox | None = None,
        driver: NativePluginDriver | None = None,
    ) -> None:
        super().__init__()
        self.config = config
        self.root = Path(root)
        self.framework_version = distribution_version("arclet-entari")
        self.store = (
            store
            if store is not None
            else WorkshopStore(
                self.root,
                max_total_bytes=config.max_total_source_bytes,
                max_versions=config.max_versions_per_plugin,
            )
        )
        if sandbox is None:
            from utils.plugin_workshop_sandbox.docker import DockerSandbox

            sandbox = DockerSandbox(config.sandbox_image, framework_version=self.framework_version)
        self.sandbox = sandbox
        self.driver = driver if driver is not None else NativePluginDriver(self.root)
        self._lifecycle_lock = asyncio.Lock()
        self._validation_lock = asyncio.Lock()
        self._prepare_lock = asyncio.Lock()
        self._prepared = asyncio.Event()
        self._prepare_error = ""
        self._ready = False
        self._closing = False
        self._closed = False
        self._admitted = 0
        self._validations: dict[asyncio.Task, asyncio.Event] = {}
        self._changes: set[asyncio.Task] = set()
        self._writes: set[asyncio.Task] = set()
        self._close_task: asyncio.Task | None = None
        self._restoration_errors: dict[str, str] = {}
        self._admissions_drained = asyncio.Event()
        self._admissions_drained.set()

    @property
    def required(self) -> set[str]:
        return set()

    @property
    def stages(self) -> set[Phase]:
        return {"preparing", "blocking", "cleanup"}

    async def _write(self, function: Callable, *args, on_complete: Callable | None = None, **kwargs):
        async def invoke():
            result = await asyncio.to_thread(function, *args, **kwargs)
            if on_complete is not None:
                on_complete(result)
            return result

        task = asyncio.create_task(invoke())
        self._writes.add(task)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await _settle(task)
            raise
        finally:
            self._writes.discard(task)

    def _ensure_running(self) -> None:
        if self._closing or not self._ready:
            raise WorkshopError("Workshop is not ready or is shutting down", code="workshop_unavailable", status=503)

    @staticmethod
    def _require_admin(actor: Actor) -> None:
        if not actor.is_admin:
            raise WorkshopError("Operator approval is required", code="forbidden", status=403)

    async def prepare(self) -> None:
        async with self._prepare_lock:
            if self._prepared.is_set():
                return
            try:
                await self._write(self.store.initialize)
                for operation in await asyncio.to_thread(self.store.pending_operations):
                    await self._write(
                        self.store.complete_change,
                        operation.operation_id,
                        success=False,
                        error="Interrupted before durable commit; restoring last committed enabled version",
                    )
                self.driver.prepare()
                self.driver.reconcile()
            except Exception as error:
                self._prepare_error = str(error)
            finally:
                self._prepared.set()

    async def restore(self) -> None:
        """Called by late Ready, never while the manager is waiting for prepare."""
        await self._prepared.wait()
        async with self._lifecycle_lock:
            if self._ready or self._closing or self._prepare_error:
                return
            for record in await asyncio.to_thread(self.store.active_versions):
                try:
                    self._approved(record, record.source_hash)
                    files = await asyncio.to_thread(self.store.read_files, record.plugin_name, record.version, _SYSTEM)
                    await self.driver.apply(record, files)
                    await self._write(self.store.record_runtime_error, record.plugin_name, "")
                except Exception as error:
                    detail = f"Committed approved version could not be restored: {error}"
                    self._restoration_errors[record.plugin_name] = detail
                    await self._write(self.store.record_runtime_error, record.plugin_name, detail)
            self._ready = True

    async def launch(self, manager: Launart):
        async with self.stage("preparing"):
            await self.prepare()
        async with self.stage("blocking"):
            await manager.status.wait_for_sigexit()
        async with self.stage("cleanup"):
            await self.aclose()

    async def get_status(self) -> dict[str, object]:
        return {
            "ready": self._ready and not self._closing,
            "running": self._prepared.is_set() and not self._closed,
            "sandbox": await self.sandbox.status(),
            "framework_version": self.framework_version,
            "error": self._prepare_error,
            "restoration_errors": dict(self._restoration_errors),
            "validation_jobs": self._admitted,
            "native_permissions_warning": NATIVE_PERMISSIONS_WARNING,
        }

    def _admit(self) -> None:
        self._ensure_running()
        if self._admitted >= 8:
            raise WorkshopError("Workshop validation queue is full", code="workshop_busy", status=429)
        self._admitted += 1
        self._admissions_drained.clear()

    async def submit(
        self,
        plugin_name: str,
        files: Mapping[str, str],
        manifest: Mapping[str, object],
        actor: Actor,
        *,
        on_created: Callable[[VersionRecord], None] | None = None,
    ) -> VersionRecord:
        parsed = parse_manifest(manifest)
        self._admit()
        created: VersionRecord | None = None
        validation_started = False

        def saved(record: VersionRecord) -> None:
            nonlocal created
            created = record
            if on_created is not None:
                on_created(record)

        try:
            record = await self._write(self.store.create_version, plugin_name, files, parsed, actor, on_complete=saved)
            validation_started = True
            return await self._validate_owned(record.plugin_name, record.version, actor)
        except BaseException:
            if created is not None and not validation_started:
                await self._write(self.store.mark_validating, created.plugin_name, created.version)
                await self._write(
                    self.store.record_report,
                    created.plugin_name,
                    created.version,
                    ValidationReport(
                        "cancelled",
                        created.source_hash,
                        (CheckResult("submission", False, "Submission ended after durable candidate creation"),),
                        framework_version=self.framework_version,
                    ),
                )
            raise
        finally:
            self._admitted -= 1
            if not self._admitted:
                self._admissions_drained.set()

    async def validate(self, plugin_name: str, version: int, actor: Actor) -> VersionRecord:
        self._admit()
        try:
            return await self._validate_owned(plugin_name, version, actor)
        finally:
            self._admitted -= 1
            if not self._admitted:
                self._admissions_drained.set()

    async def _validate_owned(self, name: str, version: int, actor: Actor) -> VersionRecord:
        initialized = asyncio.Event()
        task = asyncio.create_task(self._run_validation(name, version, actor, initialized))
        self._validations[task] = initialized
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await _settle(asyncio.create_task(initialized.wait()))
            task.cancel()
            try:
                await _settle(task)
            except asyncio.CancelledError:
                pass
            raise
        finally:
            self._validations.pop(task, None)

    async def _run_validation(
        self,
        name: str,
        version: int,
        actor: Actor,
        initialized: asyncio.Event,
    ) -> VersionRecord:
        record: VersionRecord | None = None
        marked = False

        def mark_started(_result) -> None:
            nonlocal marked
            marked = True
            initialized.set()

        try:
            record = await asyncio.to_thread(self.store.get_version, name, version, actor)
            async with self._lifecycle_lock:
                await self._write(self.store.mark_validating, name, version, on_complete=mark_started)
            async with self._validation_lock:
                if self._closing:
                    raise asyncio.CancelledError
                files = await asyncio.to_thread(self.store.read_files, name, version, actor)
                request = SandboxRequest(
                    name,
                    record.source_hash,
                    files,
                    record.manifest,
                    occupied_commands=self.driver.occupied_commands(name),
                    limits=SandboxLimits(
                        timeout_seconds=self.config.sandbox_timeout_seconds,
                        memory_mb=self.config.sandbox_memory_mb,
                        cpus=self.config.sandbox_cpus,
                    ),
                )
                report = await self.sandbox.validate(request)
                if report.source_hash != record.source_hash:
                    raise WorkshopError("Acceptance report has the wrong source digest", code="report_mismatch")
                if report.passed and report.framework_version != self.framework_version:
                    raise WorkshopError("Acceptance framework differs from native host", code="framework_mismatch")
        except BaseException as error:
            if not marked or record is None:
                raise
            cancelled = isinstance(error, asyncio.CancelledError)
            report = ValidationReport(
                "cancelled" if cancelled else "failed",
                record.source_hash,
                (CheckResult("acceptance", False, "Acceptance cancelled" if cancelled else str(error)[:2000]),),
                framework_version=self.framework_version,
            )
            result = await self._write(self.store.record_report, name, version, report)
            if cancelled:
                raise
            return result
        finally:
            initialized.set()
        return await self._write(self.store.record_report, name, version, report)

    async def list_projects(self, actor: Actor, *, limit: int = 100, offset: int = 0) -> list[dict[str, object]]:
        self._ensure_running()
        return await asyncio.to_thread(self.store.list_projects, actor, limit=limit, offset=offset)

    async def list_versions(self, plugin_name: str, actor: Actor) -> list[VersionRecord]:
        self._ensure_running()
        return await asyncio.to_thread(self.store.list_versions, plugin_name, actor)

    async def detail(self, plugin_name: str, version: int, actor: Actor) -> VersionRecord:
        self._ensure_running()
        return await asyncio.to_thread(self.store.get_version, plugin_name, version, actor)

    async def source(self, plugin_name: str, version: int, actor: Actor) -> dict[str, str]:
        self._ensure_running()
        return await asyncio.to_thread(self.store.read_files, plugin_name, version, actor)

    async def approve(self, plugin_name: str, version: int, source_hash: str, actor: Actor) -> VersionRecord:
        self._require_admin(actor)
        async with self._lifecycle_lock:
            self._ensure_running()
            record = await asyncio.to_thread(self.store.get_version, plugin_name, version, actor)
            self._accepted(record, source_hash)
            return await self._write(self.store.approve, plugin_name, version, source_hash, actor)

    def _accepted(self, record: VersionRecord, source_hash: str) -> None:
        report = record.report
        if (
            source_hash != record.source_hash
            or report is None
            or not report.passed
            or report.source_hash != source_hash
        ):
            raise WorkshopError(
                "Exact immutable version lacks successful acceptance", code="acceptance_required", status=409
            )
        if report.framework_version != self.framework_version:
            raise WorkshopError(
                "Approved version requires acceptance against the current framework",
                code="framework_mismatch",
                status=409,
            )
        if not report.image_id.startswith("sha256:") or not report.python_version.startswith("3.10."):
            raise WorkshopError(
                "Acceptance evidence lacks pinned isolation provenance", code="report_mismatch", status=409
            )

    def _approved(self, record: VersionRecord, source_hash: str) -> None:
        self._accepted(record, source_hash)
        if not record.approved_by or not record.approved_at:
            raise WorkshopError(
                "Exact immutable version requires operator approval", code="approval_required", status=409
            )

    async def activate(
        self,
        plugin_name: str,
        version: int,
        source_hash: str,
        actor: Actor,
        *,
        on_changed: Callable[[ProjectState], None] | None = None,
    ) -> ProjectState:
        return await self._change("activate", plugin_name, version, source_hash, actor, on_changed)

    async def rollback(
        self,
        plugin_name: str,
        version: int,
        source_hash: str,
        actor: Actor,
        *,
        on_changed: Callable[[ProjectState], None] | None = None,
    ) -> ProjectState:
        return await self._change("rollback", plugin_name, version, source_hash, actor, on_changed)

    async def disable(self, plugin_name: str, actor: Actor) -> ProjectState:
        return await self._change("disable", plugin_name, None, "", actor)

    async def _change(
        self,
        action: str,
        name: str,
        version: int | None,
        source_hash: str,
        actor: Actor,
        on_changed: Callable[[ProjectState], None] | None = None,
    ) -> ProjectState:
        self._require_admin(actor)
        self._ensure_running()
        task = asyncio.create_task(self._run_change(action, name, version, source_hash, actor, on_changed))
        self._changes.add(task)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await _settle(task)
            raise
        finally:
            self._changes.discard(task)

    async def _run_change(
        self,
        action: str,
        name: str,
        version: int | None,
        source_hash: str,
        actor: Actor,
        on_changed: Callable[[ProjectState], None] | None,
    ) -> ProjectState:
        async with self._lifecycle_lock:
            self._ensure_running()
            operation = await self._write(self.store.begin_change, name, version, source_hash, actor, action=action)
            previous: VersionRecord | None = None
            previous_files: dict[str, str] | None = None
            changed = False
            committed = False
            try:
                if (
                    operation.previous_enabled
                    and operation.previous_version is not None
                    and self.driver.is_loaded(name)
                ):
                    try:
                        previous = await asyncio.to_thread(
                            self.store.get_version, name, operation.previous_version, actor
                        )
                        self._approved(previous, previous.source_hash)
                        previous_files = await asyncio.to_thread(self.store.read_files, name, previous.version, actor)
                    except WorkshopError:
                        if version is not None:
                            raise
                        # A broken or obsolete approval must never prevent an operator from disabling code.
                        previous = None
                if version is None:
                    changed = True
                    await self.driver.disable(name)
                else:
                    target = await asyncio.to_thread(self.store.get_version, name, version, actor)
                    self._approved(target, source_hash)
                    files = await asyncio.to_thread(self.store.read_files, name, version, actor)
                    if not (target.active and self.driver.is_loaded(name, version)):
                        await self.driver.apply(target, files)
                        changed = True
                state = await self._write(self.store.complete_change, operation.operation_id, success=True)
                committed = True
                if on_changed is not None:
                    on_changed(state)
                self._restoration_errors.pop(name, None)
                return state
            except BaseException as error:
                if committed:
                    raise
                detail = str(error)[:2000] or "Native change cancelled"
                candidate_left_running = (
                    version is not None
                    and version != operation.previous_version
                    and self.driver.is_loaded(name, version)
                )
                if changed or candidate_left_running:
                    try:
                        if previous is not None and previous_files is not None:
                            await self.driver.apply(previous, previous_files)
                        else:
                            await self.driver.disable(name)
                    except BaseException:
                        detail += "; prior runtime restoration failed; committed state retained for restart recovery"
                await self._write(self.store.complete_change, operation.operation_id, success=False, error=detail)
                if isinstance(error, asyncio.CancelledError):
                    raise
                raise WorkshopError(detail, code="native_change_failed", status=409) from error

    async def aclose(self) -> None:
        if self._close_task is None:
            self._closing = True
            self._close_task = asyncio.create_task(self._close())
        await _settle(self._close_task)

    async def _close(self) -> None:
        try:
            for task, initialized in tuple(self._validations.items()):
                await initialized.wait()
                task.cancel()
            await asyncio.gather(*tuple(self._validations), return_exceptions=True)
            await self._admissions_drained.wait()
            try:
                await self.sandbox.aclose()
            finally:
                await asyncio.gather(*tuple(self._changes), return_exceptions=True)
                await asyncio.gather(*tuple(self._writes), return_exceptions=True)
                async with self._lifecycle_lock:
                    await self.driver.aclose()
        finally:
            self._ready = False
            self._closed = True
