"""Generation-local agent session access for history-aware tools."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from collections.abc import Iterator


@dataclass(frozen=True, slots=True)
class AgentAccessContext:
    scope_id: int
    session_id: int
    turn_id: int
    user_id: str
    allow_archived_sessions: bool = False
    allow_payload_delivery: bool = False
    allow_context_pin: bool = False


_ACTIVE_AGENT_CONTEXT: ContextVar[AgentAccessContext | None] = ContextVar(
    "llm_chat_agent_access_context",
    default=None,
)


@contextmanager
def agent_access_scope(context: AgentAccessContext) -> Iterator[None]:
    token = _ACTIVE_AGENT_CONTEXT.set(context)
    try:
        yield
    finally:
        _ACTIVE_AGENT_CONTEXT.reset(token)


def current_agent_access() -> AgentAccessContext | None:
    return _ACTIVE_AGENT_CONTEXT.get()
