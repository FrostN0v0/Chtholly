"""Presentation helpers for status snapshots."""

from __future__ import annotations

from .models import HealthSummary, StatusSnapshot

_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def clamp_percent(value: float) -> float:
    return min(100.0, max(0.0, float(value)))


def format_bytes(value: float) -> str:
    amount = max(0.0, float(value))
    unit = _UNITS[0]
    for unit in _UNITS:
        if amount < 1024.0 or unit == _UNITS[-1]:
            break
        amount /= 1024.0
    if unit == "B":
        return f"{amount:.0f} {unit}"
    return f"{amount:.1f} {unit}"


def format_rate(value: float) -> str:
    return f"{format_bytes(value)}/s"


def format_duration(value: float) -> str:
    seconds = max(0, int(value))
    days, remainder = divmod(seconds, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, seconds = divmod(remainder, 60)
    if days:
        return f"{days}d {hours:02d}h {minutes:02d}m"
    if hours:
        return f"{hours}h {minutes:02d}m {seconds:02d}s"
    return f"{minutes}m {seconds:02d}s"


def assess_health(snapshot: StatusSnapshot) -> HealthSummary:
    swap_percent = snapshot.swap.percent if snapshot.swap.total_bytes else 0.0
    peak = max(snapshot.cpu.percent, snapshot.memory.percent, swap_percent, snapshot.disk.percent)
    if peak >= 92.0:
        return HealthSummary("critical", "Under pressure", "One or more resources are close to capacity.")
    if peak >= 75.0:
        return HealthSummary("warning", "Busy", "Resource usage is elevated but still available.")
    return HealthSummary("healthy", "Healthy", "Resources are within their normal operating range.")


def format_plain_status(snapshot: StatusSnapshot) -> str:
    return "\n".join(
        (
            (
                f"CPU {snapshot.cpu.percent:.1f}% | RAM {format_bytes(snapshot.memory.used_bytes)} / "
                f"{format_bytes(snapshot.memory.total_bytes)} ({snapshot.memory.percent:.1f}%)"
            ),
            (
                f"Disk {format_bytes(snapshot.disk.used_bytes)} / {format_bytes(snapshot.disk.total_bytes)} "
                f"({snapshot.disk.percent:.1f}%) | Swap {snapshot.swap.percent:.1f}%"
            ),
            (
                f"Network up {format_rate(snapshot.network.upload_bytes_per_second)} | "
                f"down {format_rate(snapshot.network.download_bytes_per_second)}"
            ),
            (
                f"Host uptime {format_duration(snapshot.system_uptime_seconds)} | "
                f"Bot uptime {format_duration(snapshot.process.uptime_seconds)}"
            ),
        )
    )
