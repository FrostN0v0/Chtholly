"""Durable scope selection and immutable historical persona snapshots."""

from __future__ import annotations

import json
import asyncio
from datetime import datetime
from contextlib import asynccontextmanager
from dataclasses import field, dataclass
from collections.abc import AsyncIterator

from entari_plugin_database import get_session

from .models import ScopePersonaSelection, SessionPersonaSnapshot
from .core.personality import ResolvedPersona, PersonaConfiguration, resolve_persona


@dataclass(slots=True)
class _ScopeLock:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    owner: asyncio.Task | None = None
    users: int = 0


_scope_locks: dict[int, _ScopeLock] = {}


@asynccontextmanager
async def persona_scope_lock(scope_id: int) -> AsyncIterator[None]:
    """Serialize short selection/session boundaries, reentrant only in the owning task."""
    entry = _scope_locks.get(scope_id)
    if entry is None:
        entry = _ScopeLock()
        _scope_locks[scope_id] = entry
    entry.users += 1
    task = asyncio.current_task()
    acquired = False
    try:
        if entry.owner is not task:
            await entry.lock.acquire()
            acquired = True
            entry.owner = task
        yield
    finally:
        if acquired:
            entry.owner = None
            entry.lock.release()
        entry.users -= 1
        if not entry.users:
            _scope_locks.pop(scope_id, None)


async def resolve_scope_persona(config: PersonaConfiguration, scope_id: int) -> ResolvedPersona:
    """Read the effective selection without mutating absent/deleted selections."""
    async with persona_scope_lock(scope_id):
        async with get_session() as db:
            selection = await db.get(ScopePersonaSelection, scope_id)
            key = selection.persona_key if selection else None
        if key not in config.personas:
            key = None
        return resolve_persona(config, key)


async def select_scope_persona(config: PersonaConfiguration, scope_id: int, key: str) -> ResolvedPersona:
    """Persist a validated selection; callers own any associated session transition."""
    persona = resolve_persona(config, key)
    async with persona_scope_lock(scope_id):
        async with get_session() as db:
            selection = await db.get(ScopePersonaSelection, scope_id)
            if selection is None:
                db.add(ScopePersonaSelection(scope_id=scope_id, persona_key=persona.key))
            elif selection.persona_key != persona.key:
                selection.persona_key = persona.key
                selection.updated_at = datetime.utcnow()
            else:
                return persona
            await db.commit()
    return persona


async def remember_session_persona(session_id: int, persona: ResolvedPersona) -> None:
    """Record a newly created session's identity once; never relabel historical sessions."""
    async with get_session() as db:
        existing = await db.get(SessionPersonaSnapshot, session_id)
        if existing is not None:
            if existing.snapshot_json != persona.baseline_text:
                raise ValueError("A session's recorded persona cannot be changed")
            return
        db.add(SessionPersonaSnapshot(session_id=session_id, snapshot_json=persona.baseline_text))
        await db.commit()


async def session_persona(session_id: int) -> dict[str, str | None] | None:
    async with get_session() as db:
        snapshot = await db.get(SessionPersonaSnapshot, session_id)
        if snapshot is None:
            return None
        return json.loads(snapshot.snapshot_json)
