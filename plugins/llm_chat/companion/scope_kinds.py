"""Host-observed native channel kinds; never infer private scope from an ID."""

from __future__ import annotations

from typing import Any

from satori import Channel, ChannelType

from .types import CompanionConflict
from ..models import ChatScope
from .persistence import transaction
from .scope_kind_models import CompanionScopeKind


async def bind_scope_channel(scope: ChatScope, channel: Channel, *, session_factory: Any = None) -> None:
    """Remember only an exact native lookup or actual inbound channel observation."""
    if not isinstance(channel, Channel) or channel.id != scope.channel_id:
        raise CompanionConflict("Native channel does not match its companion scope")
    if channel.type not in {ChannelType.TEXT, ChannelType.DIRECT}:
        raise CompanionConflict("Companion scope requires a native text or direct channel")
    kind = "direct" if channel.type == ChannelType.DIRECT else "text"
    async with transaction(session_factory) as db:
        connection = await db.connection()
        await connection.run_sync(lambda conn: CompanionScopeKind.__table__.create(conn, checkfirst=True))
        current = await db.get(ChatScope, scope.id)
        if current is None or (current.platform, current.account_id, current.channel_id) != (
            scope.platform,
            scope.account_id,
            channel.id,
        ):
            raise CompanionConflict("Native channel scope changed before binding")
        row = await db.get(CompanionScopeKind, scope.id)
        if row is None:
            db.add(CompanionScopeKind(scope_id=scope.id, kind=kind))
        elif row.kind != kind:
            raise CompanionConflict("Native companion channel kind changed")


async def scope_is_private(db: Any, scope_id: int) -> bool:
    row = await db.get(CompanionScopeKind, scope_id)
    if row is None or row.kind not in {"text", "direct"}:
        raise CompanionConflict("Companion scope has no host-observed native channel kind")
    return row.kind == "direct"
