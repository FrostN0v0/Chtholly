"""Actual transport availability for host-owned companion work."""

from __future__ import annotations

import time
from typing import Any
import asyncio
from collections.abc import Mapping, Callable

from satori import LoginStatus


class AccountAvailability:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._status: dict[int, tuple[float, bool]] = {}

    async def online(self, account: Any, *, fresh: bool = False) -> bool:
        connected = getattr(account, "connected", None)
        server = getattr(getattr(account, "protocol", None), "server", None)
        if server is None:
            return connected is not None and connected.is_set()
        from entari_plugin_server.patch import DirectAdapterServer

        if not isinstance(server, DirectAdapterServer):
            return connected is not None and connected.is_set()
        if str(account.platform) != "onebot":
            return getattr(account.self_info, "status", None) == LoginStatus.ONLINE
        key = id(account)
        moment = self._clock()
        previous = self._status.get(key)
        if not fresh and previous is not None and moment - previous[0] < 5.0:
            return previous[1]
        # The reverse OneBot driver can retain OFFLINE after reconnect. Its native
        # status action is authoritative; never mutate or invent the registry login.
        try:
            result = await asyncio.wait_for(account.protocol.internal("get_status"), timeout=5.0)
        except asyncio.CancelledError:
            raise
        except Exception:
            ready = False
        else:
            data = result.get("data", result) if isinstance(result, Mapping) else None
            ready = isinstance(data, Mapping) and data.get("online") is True and data.get("good") is not False
        self._status[key] = (moment, ready)
        return ready

    def clear(self) -> None:
        self._status.clear()
