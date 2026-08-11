from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from utils.status_core import (
    CpuMetric,
    UsageMetric,
    NetworkMetric,
    ProcessMetric,
    StatusSnapshot,
    StatusCollectionError,
    format_rate,
    format_bytes,
    assess_health,
    collect_status,
    format_duration,
    format_plain_status,
    normalize_sample_interval,
)


class FakeProbe:
    def __init__(self, *, fail_disk: bool = False, decreasing_network: bool = False) -> None:
        self.fail_disk = fail_disk
        self.decreasing_network = decreasing_network
        self.network_calls = 0
        self.sampled_interval: float | None = None
        self.process_primed = False

    def prime_process_cpu(self) -> None:
        self.process_primed = True

    def sample_cpu_percent(self, interval: float) -> float:
        self.sampled_interval = interval
        return 123.0

    def cpu_model(self) -> str:
        return "Test CPU"

    def cpu_counts(self) -> tuple[int | None, int | None]:
        return 4, 8

    def cpu_frequency_mhz(self) -> float | None:
        return 3200.0

    def memory(self) -> UsageMetric:
        return UsageMetric(50.0, 4 * 1024**3, 8 * 1024**3)

    def swap(self) -> UsageMetric:
        return UsageMetric(0.0, 0, 0)

    def disk(self, path: str) -> UsageMetric:
        if self.fail_disk:
            raise OSError(path)
        return UsageMetric(25.0, 25 * 1024**3, 100 * 1024**3)

    def disk_mount(self, path: str) -> str:
        return "C:\\"

    def network_totals(self) -> tuple[int, int]:
        samples = ((1000, 2000), (900, 1800)) if self.decreasing_network else ((1000, 2000), (1600, 3200))
        sample = samples[min(self.network_calls, len(samples) - 1)]
        self.network_calls += 1
        return sample

    def process(self, now: float) -> ProcessMetric:
        return ProcessMetric(42, 12.5, 256 * 1024**2, 9, 500.0)

    def system_uptime(self, now: float) -> float:
        return 9000.0

    def os_info(self) -> tuple[str, str, str]:
        return "TestOS", "1.0", "x86_64"

    def python_version(self) -> str:
        return "3.14.0"


def _snapshot(*, cpu: float = 20.0, memory: float = 40.0, swap: float = 0.0, disk: float = 30.0) -> StatusSnapshot:
    from datetime import datetime, timezone

    return StatusSnapshot(
        generated_at=datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc),
        os_name="TestOS",
        os_release="1.0",
        architecture="x86_64",
        python_version="3.14.0",
        framework_version="0.18.6",
        plugin_count=7,
        system_uptime_seconds=9000.0,
        disk_mount="C:\\",
        cpu=CpuMetric(cpu, "Test CPU", 4, 8, 3200.0),
        memory=UsageMetric(memory, 4 * 1024**3, 8 * 1024**3),
        swap=UsageMetric(swap, 0 if swap == 0 else 1024**3, 0 if swap == 0 else 2 * 1024**3),
        disk=UsageMetric(disk, 25 * 1024**3, 100 * 1024**3),
        network=NetworkMetric(1024.0, 2048.0, 10_000, 20_000),
        process=ProcessMetric(42, 12.5, 256 * 1024**2, 9, 500.0),
    )


def test_formatters_cover_binary_units_and_duration_boundaries() -> None:
    assert format_bytes(1536) == "1.5 KiB"
    assert format_rate(2048) == "2.0 KiB/s"
    assert format_duration(65) == "1m 05s"
    assert format_duration(90_061) == "1d 01h 01m"


def test_health_uses_capacity_thresholds_and_ignores_missing_swap() -> None:
    healthy = _snapshot()
    assert assess_health(healthy).code == "healthy"
    assert assess_health(replace(healthy, memory=replace(healthy.memory, percent=80.0))).code == "warning"
    assert assess_health(replace(healthy, disk=replace(healthy.disk, percent=95.0))).code == "critical"


def test_collect_status_clamps_samples_and_calculates_rates() -> None:
    probe = FakeProbe()
    moments = iter((5.0, 5.5))
    snapshot = asyncio.run(
        collect_status(
            disk_path=".",
            sample_interval=0.01,
            framework_version="0.18.6",
            plugin_count=7,
            probe=probe,
            wall_time=lambda: 1_800_000_000.0,
            monotonic=lambda: next(moments),
        )
    )

    assert probe.process_primed
    assert probe.sampled_interval == 0.1
    assert snapshot.cpu.percent == 100.0
    assert snapshot.network.upload_bytes_per_second == 1200.0
    assert snapshot.network.download_bytes_per_second == 2400.0
    assert snapshot.process.uptime_seconds == 500.0
    assert snapshot.disk_mount == "C:\\"
    assert snapshot.plugin_count == 7


def test_collect_status_clamps_counter_rollover_to_zero() -> None:
    probe = FakeProbe(decreasing_network=True)
    moments = iter((1.0, 1.1))
    snapshot = asyncio.run(
        collect_status(
            disk_path=".",
            sample_interval=0.1,
            framework_version="0.18.6",
            plugin_count=1,
            probe=probe,
            wall_time=lambda: 1_800_000_000.0,
            monotonic=lambda: next(moments),
        )
    )

    assert snapshot.network.upload_bytes_per_second == 0.0
    assert snapshot.network.download_bytes_per_second == 0.0


def test_collect_status_wraps_probe_failures() -> None:
    with pytest.raises(StatusCollectionError):
        asyncio.run(
            collect_status(
                disk_path="missing",
                sample_interval=0.5,
                framework_version="0.18.6",
                plugin_count=1,
                probe=FakeProbe(fail_disk=True),
            )
        )


def test_plain_fallback_keeps_core_metrics() -> None:
    text = format_plain_status(_snapshot())
    assert "CPU 20.0%" in text
    assert "RAM 4.0 GiB / 8.0 GiB" in text
    assert "Network up 1.0 KiB/s" in text


def test_sample_interval_has_safe_bounds() -> None:
    assert normalize_sample_interval(-1.0) == 0.1
    assert normalize_sample_interval(0.5) == 0.5
    assert normalize_sample_interval(10.0) == 2.0
