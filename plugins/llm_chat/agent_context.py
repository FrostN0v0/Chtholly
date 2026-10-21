"""Generation-local agent session access for history-aware tools."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from collections.abc import Iterator


@dataclass(frozen=True, slots=True)
class ContextReadReference:
    """An exact descriptor emitted by the host, never recovered from message text."""

    event_ref: str
    path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class ContextReadGrant:
    scope_id: int
    session_id: int
    generation_session_id: int
    generation_turn_id: int
    event_ref: str
    execution_ref: str
    path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class AgentAccessContext:
    scope_id: int
    session_id: int
    turn_id: int
    user_id: str
    allow_archived_sessions: bool = False
    allow_payload_delivery: bool = False
    allow_context_pin: bool = False
    raw_user_text: str = ""
    is_operator: bool = False
    context_read_grants: tuple[ContextReadGrant, ...] = ()
    trigger_kind: str = "message"
    reminder_task_ref: str | None = None
    capability_names: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if len(self.capability_names) > 16 or any(
            not isinstance(name, str) or not name or len(name) > 64 for name in self.capability_names
        ):
            raise ValueError("Generation capability names must be bounded")


SCHEDULED_TOOL_CAPABILITIES = frozenset(
    {
        "reminder",
        "reminder.resolve",
        "safe_read",
        "safe_prepare",
        "web_read",
        "send_msg",
    }
)

AUTONOMOUS_TOOL_CAPABILITIES = frozenset({"safe_read", "safe_prepare", "web_read", "send_msg"})


_ACTIVE_AGENT_CONTEXT: ContextVar[AgentAccessContext | None] = ContextVar(
    "llm_chat_agent_access_context",
    default=None,
)


@dataclass(slots=True)
class _AgentAccessLifetime:
    active: bool = True


_ACTIVE_AGENT_LIFETIME: ContextVar[_AgentAccessLifetime | None] = ContextVar(
    "llm_chat_agent_access_lifetime", default=None
)


def current_agent_access_is_live() -> bool:
    """Reject inherited access after its owning generation scope has exited."""
    lifetime = _ACTIVE_AGENT_LIFETIME.get()
    return lifetime is not None and lifetime.active


@contextmanager
def agent_access_scope(context: AgentAccessContext) -> Iterator[None]:
    token = _ACTIVE_AGENT_CONTEXT.set(context)
    lifetime = _AgentAccessLifetime()
    lifetime_token = _ACTIVE_AGENT_LIFETIME.set(lifetime)
    try:
        yield
    finally:
        _ACTIVE_AGENT_CONTEXT.reset(token)
        lifetime.active = False
        _ACTIVE_AGENT_LIFETIME.reset(lifetime_token)


def current_agent_access() -> AgentAccessContext | None:
    original = _ACTIVE_AGENT_CONTEXT.get()
    if original is None:
        return None
    from .steering import current_steering

    state = current_steering()
    access = state.access if state is not None and not state.closed else None
    if access is not None and (
        access.scope_id,
        access.session_id,
        access.turn_id,
        access.user_id,
        access.trigger_kind,
    ) == (original.scope_id, original.session_id, original.turn_id, original.user_id, original.trigger_kind):
        return access
    return original
