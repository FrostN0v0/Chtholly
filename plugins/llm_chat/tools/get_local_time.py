"""get_local_time LLM tool implementation."""

from __future__ import annotations

import json
from datetime import tzinfo, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from dataclasses import dataclass
from collections.abc import Callable

from arclet.letoderea import Subscriber
from arclet.entari.plugin.model import PluginDispatcher

from ..core.types import JSONType
from ._registration import register_tool

Clock = Callable[[tzinfo | None], datetime]


@dataclass
class LocalTimeToolContext:
    """Mutable clock dependency for deterministic current-time lookup."""

    now: Clock


def _format_utc_offset(value: datetime) -> str:
    compact = value.strftime("%z")
    if len(compact) != 5:
        return compact
    return f"{compact[:3]}:{compact[3:]}"


def register_get_local_time(
    dispatcher: PluginDispatcher[JSONType],
    runtime: LocalTimeToolContext,
) -> Subscriber[JSONType]:
    """Register current local or IANA-zone time lookup."""

    async def get_local_time(timezone: str = "") -> str:
        """Get the current date and time instead of guessing from model knowledge.

        Call this for current-time, current-date, weekday, UTC-offset, or timezone-sensitive questions. With an empty
        timezone, use the bot host's local timezone. When the user names a location or timezone, pass its IANA name,
        such as Asia/Shanghai, Europe/London, or America/New_York.

        Args:
            timezone (str): Optional IANA timezone name. Defaults to the bot host's local timezone.
        Returns:
            str: Compact JSON containing timezone, ISO datetime, date, time, weekday, and UTC offset.
        """

        requested = timezone.strip() if isinstance(timezone, str) else ""
        if len(requested) > 128:
            raise ValueError("Unknown IANA timezone")
        if requested:
            try:
                zone = ZoneInfo(requested)
            except (ValueError, ZoneInfoNotFoundError) as exc:
                raise ValueError("Unknown IANA timezone") from exc
            current = runtime.now(zone)
            zone_name = requested
        else:
            current = runtime.now(None)
            if current.tzinfo is None:
                current = current.astimezone()
            zone_name = getattr(current.tzinfo, "key", None) or current.tzname() or "system-local"

        return json.dumps(
            {
                "timezone": zone_name,
                "datetime": current.isoformat(timespec="seconds"),
                "date": current.date().isoformat(),
                "time": current.time().isoformat(timespec="seconds"),
                "weekday": current.strftime("%A"),
                "utc_offset": _format_utc_offset(current),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    return register_tool(dispatcher, get_local_time)
