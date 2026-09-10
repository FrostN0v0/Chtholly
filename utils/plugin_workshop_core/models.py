"""Framework-independent contracts for reviewed, immutable native plugins."""

from __future__ import annotations

from typing import Protocol
from dataclasses import field, dataclass
from collections.abc import Mapping, Callable


class WorkshopError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid_request", status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True, slots=True)
class Actor:
    key: str
    scope_id: int | None = None
    is_admin: bool = False


@dataclass(frozen=True, slots=True)
class CommandCheck:
    command: str
    expected_contains: str
    operator: bool = False
    repeatable: bool = True


@dataclass(frozen=True, slots=True)
class Manifest:
    title: str
    description: str
    commands: tuple[str, ...]
    permissions: tuple[str, ...]
    data_description: str
    configuration: dict[str, object]
    checks: tuple[CommandCheck, ...]


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ValidationReport:
    status: str
    source_hash: str
    checks: tuple[CheckResult, ...] = ()
    log: str = ""
    framework_version: str = ""
    python_version: str = ""
    image_id: str = ""

    @property
    def passed(self) -> bool:
        return self.status == "passed" and bool(self.checks) and all(check.passed for check in self.checks)


@dataclass(frozen=True, slots=True)
class VersionRecord:
    plugin_name: str
    version: int
    source_hash: str
    manifest: Manifest
    owner_key: str
    scope_id: int | None
    created_at: str
    validation_status: str = "pending"
    report: ValidationReport | None = None
    approved_by: str | None = None
    approved_at: str | None = None
    active: bool = False
    ever_active: bool = False


@dataclass(frozen=True, slots=True)
class ProjectState:
    plugin_name: str
    active_version: int | None
    enabled: bool
    last_error: str = ""


@dataclass(frozen=True, slots=True)
class Operation:
    operation_id: str
    plugin_name: str
    action: str
    target_version: int | None
    previous_version: int | None
    previous_enabled: bool


@dataclass(frozen=True, slots=True)
class SandboxLimits:
    timeout_seconds: float = 60.0
    memory_mb: int = 512
    cpus: float = 1.0
    pids: int = 64
    output_bytes: int = 65536


@dataclass(frozen=True, slots=True)
class SandboxRequest:
    plugin_name: str
    source_hash: str
    files: Mapping[str, str]
    manifest: Manifest
    occupied_commands: tuple[str, ...] = ()
    limits: SandboxLimits = field(default_factory=SandboxLimits)


class Sandbox(Protocol):
    async def status(self) -> dict[str, object]: ...

    async def validate(self, request: SandboxRequest) -> ValidationReport: ...

    async def aclose(self) -> None: ...


class WorkshopAPI(Protocol):
    async def get_status(self) -> dict[str, object]: ...

    async def submit(
        self,
        plugin_name: str,
        files: Mapping[str, str],
        manifest: Mapping[str, object],
        actor: Actor,
        *,
        on_created: Callable[[VersionRecord], None] | None = None,
    ) -> VersionRecord: ...

    async def validate(self, plugin_name: str, version: int, actor: Actor) -> VersionRecord: ...

    async def list_projects(self, actor: Actor, *, limit: int = 100, offset: int = 0) -> list[dict[str, object]]: ...

    async def list_versions(self, plugin_name: str, actor: Actor) -> list[VersionRecord]: ...

    async def detail(self, plugin_name: str, version: int, actor: Actor) -> VersionRecord: ...

    async def source(self, plugin_name: str, version: int, actor: Actor) -> dict[str, str]: ...

    async def approve(self, plugin_name: str, version: int, source_hash: str, actor: Actor) -> VersionRecord: ...

    async def activate(
        self,
        plugin_name: str,
        version: int,
        source_hash: str,
        actor: Actor,
        *,
        on_changed: Callable[[ProjectState], None] | None = None,
    ) -> ProjectState: ...

    async def rollback(
        self,
        plugin_name: str,
        version: int,
        source_hash: str,
        actor: Actor,
        *,
        on_changed: Callable[[ProjectState], None] | None = None,
    ) -> ProjectState: ...

    async def disable(self, plugin_name: str, actor: Actor) -> ProjectState: ...
