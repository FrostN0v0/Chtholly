"""Cross-platform system status collection without Entari side effects."""

from __future__ import annotations

import os
import time
from typing import Protocol
import asyncio
from pathlib import Path
from datetime import datetime
import platform
from collections.abc import Callable

import psutil

from .models import CpuMetric, UsageMetric, NetworkMetric, ProcessMetric, StatusSnapshot
from .formatting import clamp_percent

_MIN_SAMPLE_INTERVAL = 0.1
_MAX_SAMPLE_INTERVAL = 2.0


class StatusCollectionError(RuntimeError):
    """Raised when the host metrics cannot be sampled."""


class StatusProbe(Protocol):
    def prime_process_cpu(self) -> None: ...

    def sample_cpu_percent(self, interval: float) -> float: ...

    def cpu_model(self) -> str: ...

    def cpu_counts(self) -> tuple[int | None, int | None]: ...

    def cpu_frequency_mhz(self) -> float | None: ...

    def memory(self) -> UsageMetric: ...

    def swap(self) -> UsageMetric: ...

    def disk(self, path: str) -> UsageMetric: ...

    def disk_mount(self, path: str) -> str: ...

    def network_totals(self) -> tuple[int, int]: ...

    def process(self, now: float) -> ProcessMetric: ...

    def system_uptime(self, now: float) -> float: ...

    def os_info(self) -> tuple[str, str, str]: ...

    def python_version(self) -> str: ...


class PsutilProbe:
    def __init__(self, process_id: int | None = None) -> None:
        self._process = psutil.Process(process_id or os.getpid())

    def prime_process_cpu(self) -> None:
        self._process.cpu_percent(interval=None)

    def sample_cpu_percent(self, interval: float) -> float:
        return float(psutil.cpu_percent(interval=interval))

    def cpu_model(self) -> str:
        candidates = (
            platform.processor(),
            os.environ.get("PROCESSOR_IDENTIFIER", ""),
            platform.uname().processor,
        )
        if model := next((item.strip() for item in candidates if item and item.strip()), ""):
            return model

        cpuinfo = Path("/proc/cpuinfo")
        if cpuinfo.is_file():
            for line in cpuinfo.read_text(encoding="utf-8", errors="ignore").splitlines():
                key, separator, value = line.partition(":")
                if separator and key.strip().lower() in {"model name", "hardware"} and value.strip():
                    return value.strip()
        return "Unknown CPU"

    def cpu_counts(self) -> tuple[int | None, int | None]:
        return psutil.cpu_count(logical=False), psutil.cpu_count(logical=True)

    def cpu_frequency_mhz(self) -> float | None:
        try:
            frequency = psutil.cpu_freq()
        except (OSError, NotImplementedError):
            return None
        return float(frequency.current) if frequency else None

    def memory(self) -> UsageMetric:
        sample = psutil.virtual_memory()
        return UsageMetric(clamp_percent(sample.percent), int(sample.used), int(sample.total))

    def swap(self) -> UsageMetric:
        sample = psutil.swap_memory()
        return UsageMetric(clamp_percent(sample.percent), int(sample.used), int(sample.total))

    def disk(self, path: str) -> UsageMetric:
        sample = psutil.disk_usage(path)
        return UsageMetric(clamp_percent(sample.percent), int(sample.used), int(sample.total))

    def disk_mount(self, path: str) -> str:
        target = os.path.normcase(os.path.abspath(path))
        candidates: list[str] = []
        for partition in psutil.disk_partitions(all=True):
            mount = os.path.normcase(os.path.abspath(partition.mountpoint))
            try:
                if os.path.commonpath((target, mount)) == mount:
                    candidates.append(partition.mountpoint)
            except ValueError:
                continue
        if candidates:
            return max(candidates, key=len)
        return Path(target).anchor or target

    def network_totals(self) -> tuple[int, int]:
        sample = psutil.net_io_counters()
        return int(sample.bytes_sent), int(sample.bytes_recv)

    def process(self, now: float) -> ProcessMetric:
        memory = self._process.memory_info()
        return ProcessMetric(
            pid=self._process.pid,
            cpu_percent=max(0.0, float(self._process.cpu_percent(interval=None))),
            memory_bytes=int(memory.rss),
            thread_count=self._process.num_threads(),
            uptime_seconds=max(0.0, now - self._process.create_time()),
        )

    def system_uptime(self, now: float) -> float:
        return max(0.0, now - psutil.boot_time())

    def os_info(self) -> tuple[str, str, str]:
        return platform.system() or "Unknown OS", platform.release() or "Unknown", platform.machine() or "Unknown"

    def python_version(self) -> str:
        return platform.python_version()


def normalize_sample_interval(value: float) -> float:
    return min(_MAX_SAMPLE_INTERVAL, max(_MIN_SAMPLE_INTERVAL, float(value)))


async def collect_status(
    *,
    disk_path: str,
    sample_interval: float,
    framework_version: str,
    plugin_count: int,
    probe: StatusProbe | None = None,
    wall_time: Callable[[], float] = time.time,
    monotonic: Callable[[], float] = time.monotonic,
) -> StatusSnapshot:
    try:
        return await _collect_status(
            disk_path=disk_path,
            sample_interval=sample_interval,
            framework_version=framework_version,
            plugin_count=plugin_count,
            probe=probe or PsutilProbe(),
            wall_time=wall_time,
            monotonic=monotonic,
        )
    except (OSError, ValueError, psutil.Error) as exc:
        raise StatusCollectionError("Unable to sample host metrics") from exc


async def _collect_status(
    *,
    disk_path: str,
    sample_interval: float,
    framework_version: str,
    plugin_count: int,
    probe: StatusProbe,
    wall_time: Callable[[], float],
    monotonic: Callable[[], float],
) -> StatusSnapshot:
    interval = normalize_sample_interval(sample_interval)
    probe.prime_process_cpu()
    sent_before, received_before = probe.network_totals()
    memory = probe.memory()
    swap = probe.swap()
    disk = probe.disk(disk_path)
    disk_mount = probe.disk_mount(disk_path)
    cpu_model = probe.cpu_model()
    physical_cores, logical_cores = probe.cpu_counts()
    frequency_mhz = probe.cpu_frequency_mhz()
    os_name, os_release, architecture = probe.os_info()
    python_version = probe.python_version()

    started = monotonic()
    cpu_percent = clamp_percent(await asyncio.to_thread(probe.sample_cpu_percent, interval))
    elapsed = max(interval, monotonic() - started)
    sent_after, received_after = probe.network_totals()
    now = wall_time()
    network = NetworkMetric(
        upload_bytes_per_second=max(0, sent_after - sent_before) / elapsed,
        download_bytes_per_second=max(0, received_after - received_before) / elapsed,
        total_sent_bytes=max(0, sent_after),
        total_received_bytes=max(0, received_after),
    )

    return StatusSnapshot(
        generated_at=datetime.fromtimestamp(now).astimezone(),
        os_name=os_name,
        os_release=os_release,
        architecture=architecture,
        python_version=python_version,
        framework_version=framework_version,
        plugin_count=max(0, plugin_count),
        system_uptime_seconds=probe.system_uptime(now),
        disk_mount=disk_mount,
        cpu=CpuMetric(
            percent=cpu_percent,
            model=cpu_model,
            physical_cores=physical_cores,
            logical_cores=logical_cores,
            frequency_mhz=frequency_mhz,
        ),
        memory=memory,
        swap=swap,
        disk=disk,
        network=network,
        process=probe.process(now),
    )
