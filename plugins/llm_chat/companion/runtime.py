"""Owned companion service access for chat and command lifecycle hooks."""

from __future__ import annotations

from typing import TYPE_CHECKING
from collections.abc import Callable

from arclet.entari import Session

if TYPE_CHECKING:
    from .service import CompanionService
    from ..agent_turn_setup import PreparedAgentTurn

_RUNTIME: CompanionService | None = None


def current_companion_service() -> CompanionService | None:
    return _RUNTIME


def install_companion_runtime(service: CompanionService) -> Callable[[], None]:
    global _RUNTIME
    _RUNTIME = service

    def dispose() -> None:
        global _RUNTIME
        if _RUNTIME is service:
            _RUNTIME = None

    return dispose


def interrupt_companion_for_input(session: Session) -> None:
    runtime = _RUNTIME
    if runtime is not None:
        runtime.interrupt_input(session)


async def record_companion_chat(session: Session, prepared: PreparedAgentTurn) -> None:
    runtime = _RUNTIME
    if runtime is None or getattr(prepared, "conversation_mode", "normal") != "companion":
        return
    await runtime.record_chat(session, prepared)
