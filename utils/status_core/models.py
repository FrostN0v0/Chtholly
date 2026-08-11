"""Immutable status snapshot models."""

from __future__ import annotations

from datetime import datetime
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class UsageMetric:
    percent: float
    used_bytes: int
    total_bytes: int


@dataclass(frozen=True, slots=True)
class CpuMetric:
    percent: float
    model: str
    physical_cores: int | None
    logical_cores: int | None
    frequency_mhz: float | None


@dataclass(frozen=True, slots=True)
class NetworkMetric:
    upload_bytes_per_second: float
    download_bytes_per_second: float
    total_sent_bytes: int
    total_received_bytes: int


@dataclass(frozen=True, slots=True)
class ProcessMetric:
    pid: int
    cpu_percent: float
    memory_bytes: int
    thread_count: int
    uptime_seconds: float


@dataclass(frozen=True, slots=True)
class StatusSnapshot:
    generated_at: datetime
    os_name: str
    os_release: str
    architecture: str
    python_version: str
    framework_version: str
    plugin_count: int
    system_uptime_seconds: float
    disk_mount: str
    cpu: CpuMetric
    memory: UsageMetric
    swap: UsageMetric
    disk: UsageMetric
    network: NetworkMetric
    process: ProcessMetric


@dataclass(frozen=True, slots=True)
class HealthSummary:
    code: str
    label: str
    detail: str
