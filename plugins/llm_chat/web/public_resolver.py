"""Public-only DNS resolver for browser screenshot transport."""

from __future__ import annotations

import socket
from typing import cast
import asyncio
import ipaddress
from collections.abc import Callable, Sequence, Awaitable

from aiohttp.abc import ResolveResult, AbstractResolver

from .policy import WebAccessError, normalize_public_url
from .screenshot_models import WebScreenshotError

AddrInfo = tuple[
    socket.AddressFamily,
    socket.SocketKind,
    int,
    str,
    tuple[str, int] | tuple[str, int, int, int],
]
ResolverLookup = Callable[
    [str, int, socket.AddressFamily],
    Awaitable[Sequence[AddrInfo]],
]


async def _default_lookup(
    host: str,
    port: int,
    family: socket.AddressFamily,
) -> Sequence[AddrInfo]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(
        host,
        port,
        family=family,
        type=socket.SOCK_STREAM,
        flags=socket.AI_ADDRCONFIG,
    )
    return cast(Sequence[AddrInfo], infos)


def _validate_host(host: str) -> None:
    try:
        ascii_host = host.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise WebScreenshotError("page host could not be resolved safely") from exc
    candidate = f"https://[{ascii_host}]/" if ":" in ascii_host else f"https://{ascii_host}/"
    try:
        normalize_public_url(candidate)
    except WebAccessError as exc:
        raise WebScreenshotError("page host is not public") from exc


def _validate_address(address: str) -> None:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError as exc:
        raise WebScreenshotError("page host resolved to an invalid address") from exc
    if (
        not parsed.is_global
        or parsed.is_loopback
        or parsed.is_private
        or parsed.is_link_local
        or parsed.is_multicast
        or parsed.is_reserved
        or parsed.is_unspecified
    ):
        raise WebScreenshotError("page host resolved to a non-public address")


class PublicResolver(AbstractResolver):
    """Resolve once, reject mixed/private answers, and pin the returned IPs."""

    def __init__(self, lookup: ResolverLookup = _default_lookup) -> None:
        self._lookup = lookup

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[ResolveResult]:
        _validate_host(host)
        try:
            infos = await self._lookup(host, port, family)
        except (OSError, asyncio.TimeoutError) as exc:
            raise WebScreenshotError("page host could not be resolved") from exc

        records: list[ResolveResult] = []
        seen: set[tuple[str, int, int]] = set()
        for resolved_family, _kind, proto, _canonical_name, socket_address in infos:
            address = socket_address[0]
            resolved_port = int(socket_address[1])
            _validate_address(address)
            key = (address, resolved_port, int(resolved_family))
            if key in seen:
                continue
            seen.add(key)
            records.append(
                ResolveResult(
                    hostname=host,
                    host=address,
                    port=resolved_port,
                    family=int(resolved_family),
                    proto=proto,
                    flags=socket.AI_NUMERICHOST | socket.AI_NUMERICSERV,
                )
            )
        if not records:
            raise WebScreenshotError("page host returned no public addresses")
        return records

    async def close(self) -> None:
        return None
